"""scripts/demo_live_fx.py

End-to-end demo of the live FX pipeline — no database and no API keys required.

It runs the *real* ingester against a live public FX feed, keeps what it fetched
in an in-memory stand-in for the ``fx_rates`` table, and then serves the real
FastAPI endpoints off that data::

    python scripts/demo_live_fx.py

Sections
--------
1. LIVE INGESTION   real HTTP fetch of NGN/KES rates, freshness-validated
2. LIVE ENDPOINTS   GET /fx/current, /fx/history, /fx/sources on those rates
3. GUARDS           the stale-rate guard and the insufficient-history guard
4. RISK SCORING     GET /risk/current — replayed over a clearly labelled
                    SIMULATED intraday series, because the keyless public feed
                    only publishes once a day and cannot support a volatility
                    score. Every rate in section 4 is synthetic and is printed
                    as such.
"""

import asyncio
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from api import fx as fx_api
from api import risk as risk_api
from api.main import app
from lib.fx_utils import market_for_source
from services import forex_ingester

client = TestClient(app)

# In-memory stand-in for the fx_rates hypertable: (timestamp, pair, source) is
# the primary key, exactly as in db/schema.sql.
STORE = {}


# ---------------------------------------------------------------------------
# In-memory fx_rates
# ---------------------------------------------------------------------------


def _save(rates):
    for timestamp, pair, rate, source in rates:
        STORE[(timestamp, pair, source)] = {
            "timestamp": timestamp,
            "pair": pair,
            "rate": rate,
            "source": source,
        }


def _rows(pair, sources=None, exclude_sources=None):
    rows = [row for row in STORE.values() if row["pair"] == pair]
    if sources:
        rows = [row for row in rows if row["source"] in sources]
    if exclude_sources:
        rows = [row for row in rows if row["source"] not in exclude_sources]
    return sorted(rows, key=lambda row: row["timestamp"])


def _latest(pair, sources=None, exclude_sources=None):
    rows = _rows(pair, sources, exclude_sources)
    return rows[-1] if rows else None


def _window(pair, since, sources=None, exclude_sources=None, limit=None):
    rows = [
        row
        for row in _rows(pair, sources, exclude_sources)
        if row["timestamp"] >= since
    ]
    return rows[-limit:] if limit else rows


def _status(pair=None):
    latest = {}
    for row in STORE.values():
        if pair and row["pair"] != pair:
            continue
        key = (row["source"], row["pair"])
        if key not in latest or row["timestamp"] > latest[key]["timestamp"]:
            latest[key] = row
    return [
        {
            "source": row["source"],
            "pair": row["pair"],
            "last_timestamp": row["timestamp"],
            "last_rate": row["rate"],
            "points_last_24h": sum(
                1
                for other in STORE.values()
                if other["source"] == row["source"]
                and other["pair"] == row["pair"]
                and other["timestamp"]
                >= datetime.now(timezone.utc) - timedelta(hours=24)
            ),
        }
        for row in latest.values()
    ]


def install_in_memory_database():
    """Points the ingester and both routers at the in-memory store."""
    forex_ingester.save_fx_rates = _save
    fx_api.get_latest_fx_rate = _latest
    fx_api.get_fx_rate_window = _window
    fx_api.get_fx_source_status = _status
    risk_api.get_fx_rate_window = _window


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def heading(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def show(method_and_path, params=None):
    response = client.get(method_and_path, params=params or {})
    query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
    print(f"\n$ GET {method_and_path}{'?' + query if query else ''}")
    print(f"→ HTTP {response.status_code}")
    print(json.dumps(response.json(), indent=2, default=str))
    return response


# ---------------------------------------------------------------------------
# Demo sections
# ---------------------------------------------------------------------------


def section_live_ingestion():
    heading("1. LIVE INGESTION — real rates fetched from the public FX feed")
    print(f"Feed:  {forex_ingester.EXCHANGERATE_API_URL}")
    print(f"Time:  {datetime.now(timezone.utc).isoformat()}\n")

    summary = asyncio.run(forex_ingester.run_ingestion())
    print(f"Ingestion summary: {summary}\n")

    if not STORE:
        print("No rates stored — the public feed is unreachable from this machine.")
        return False

    print(f"{'pair':<10}{'rate':>14}  {'source':<20}{'market':<10}quoted at")
    for row in sorted(STORE.values(), key=lambda r: r["pair"]):
        print(
            f"{row['pair']:<10}{row['rate']:>14,.4f}  {row['source']:<20}"
            f"{market_for_source(row['source']):<10}{row['timestamp'].isoformat()}"
        )
    return True


def section_live_endpoints():
    heading("2. LIVE ENDPOINTS — served from the rates fetched above")
    print(
        "\nThe same quote in both orientations, plus per-feed health. The quote is\n"
        "hours old yet not stale: this feed publishes daily and carries its own\n"
        "freshness budget (FX_DAILY_SOURCE_MAX_AGE_MINUTES), while the 15-minute\n"
        "feeds are held to 45 minutes."
    )
    show("/fx/current", {"pair": "NGN/USD"})
    show("/fx/current", {"pair": "USD/NGN"})
    show("/fx/current", {"pair": "KES/USD"})
    show("/fx/sources")


def section_guards():
    heading("3. GUARDS — the API refuses to dress stale or thin data up as live")

    # A simulated 15-minute street feed that stopped updating three hours ago.
    stale_quote = datetime.now(timezone.utc) - timedelta(hours=3)
    _save([(stale_quote, "USD/ZAR", 15.94, "SimulatedStalledFeed")])

    print(
        "\nStale-rate guard — a 15-minute feed whose last print is 3 hours old\n"
        "(simulated stall). The rate is withheld rather than served as live:"
    )
    show("/fx/current", {"pair": "USD/ZAR", "market": "parallel"})

    print("\nThe caller can still opt in, and is told exactly how old it is:")
    show(
        "/fx/current", {"pair": "USD/ZAR", "market": "parallel", "allow_stale": "true"}
    )

    print(
        "\nInsufficient-history guard — a once-a-day feed cannot support a\n"
        "volatility score, so /risk/current says so instead of inventing one:"
    )
    show("/risk/current", {"pair": "USD/NGN", "allow_stale": "true"})


def section_risk_scoring():
    heading("4. RISK SCORING — over a SIMULATED intraday parallel-market series")
    print(
        "\n!! Every rate in this section is SIMULATED, not market data. It stands in\n"
        "!! for the 15-minute parallel-market feed (PARALLEL_MARKET_API_URL) that a\n"
        "!! production deployment ingests, so the scoring path can be demonstrated\n"
        "!! without waiting 24 hours for real prints to accumulate.\n"
    )

    now = datetime.now(timezone.utc)
    for label, amplitude, pair in (
        ("calm", 0.0015, "USD/NGN"),
        ("stressed", 0.02, "USD/KES"),
    ):
        rates = [
            1750.0 * (1 + amplitude * math.sin(i / 2.0))
            if pair == "USD/NGN"
            else 132.0 * (1 + amplitude * math.sin(i / 2.0))
            for i in range(48)
        ]
        _save(
            [
                (
                    now - timedelta(minutes=15 * (len(rates) - 1 - i)),
                    pair,
                    rate,
                    "SimulatedParallelMarket",
                )
                for i, rate in enumerate(rates)
            ]
        )
        print(f"\n--- {label} {pair} series: 48 simulated points, one every 15 min ---")
        show("/risk/current", {"pair": pair, "market": "parallel", "horizon": 1})


def main():
    install_in_memory_database()
    if section_live_ingestion():
        section_live_endpoints()
        section_guards()
    section_risk_scoring()
    print("\nDemo complete.\n")


if __name__ == "__main__":
    main()
