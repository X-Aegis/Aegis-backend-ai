"""services/forex_ingester.py

Live FX rate ingester for the launch markets (NGN, KES) plus GHS/ZAR.

Every cycle (15 minutes by default) the service:

1. Fetches **official** rates from the primary vendor API, falling back to the
   next vendor when the primary is down, rate-limited or returns stale data
   (:func:`fetch_official_rates`). The chain ends at a keyless open endpoint, so
   ingestion still works on a deployment with no vendor credentials.
2. Fetches the **parallel-market** ("street") rates that people in Lagos and
   Nairobi actually transact at (:func:`fetch_parallel_market_rates`).
3. Validates every quote for freshness and sanity (:func:`validate_rates`) so a
   frozen vendor feed can never masquerade as a live rate.
4. Persists what survived into ``fx_rates`` (timestamp, pair, rate, source).

Run it as a scheduled worker::

    python -m services.forex_ingester            # loop forever, every 15 min
    python -m services.forex_ingester --once     # single cycle (cron / CI)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

# Add project root to sys.path for local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.database import save_fx_rates
from lib.fx_utils import (
    BASE_CURRENCY,
    SUPPORTED_CURRENCIES,
    ensure_utc,
    is_fresh,
    is_valid_rate,
    max_age_for_source,
    supported_pairs,
)

load_dotenv()

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FIXER_API_KEY = os.getenv("FIXER_API_KEY")
OPEN_EXCHANGE_RATES_APP_ID = os.getenv("OPEN_EXCHANGE_RATES_APP_ID")

# Parallel-market feed. Any JSON endpoint shaped like
#   {"base": "USD", "timestamp": 1755000000, "rates": {"NGN": 1750.0, "KES": 132.4}}
# works; ``timestamp`` (unix seconds or ISO-8601) is optional and defaults to now.
PARALLEL_MARKET_API_URL = os.getenv("PARALLEL_MARKET_API_URL")
PARALLEL_MARKET_API_KEY = os.getenv("PARALLEL_MARKET_API_KEY")
PARALLEL_MARKET_SOURCE = os.getenv("PARALLEL_MARKET_SOURCE", "ParallelMarket")

# Keyless open endpoint used as the last-resort official fallback.
EXCHANGERATE_API_URL = os.getenv(
    "EXCHANGERATE_API_URL", f"https://open.er-api.com/v6/latest/{BASE_CURRENCY}"
)

# Which official vendor is tried first; the remaining vendors are the fallbacks.
FX_PRIMARY_SOURCE = os.getenv("FX_PRIMARY_SOURCE", "fixer").lower()

# Target currencies and the canonical pairs they are stored as (USD/<CCY>).
CURRENCIES = list(SUPPORTED_CURRENCIES)
TRACKED_PAIRS = supported_pairs()

INGEST_INTERVAL_SECONDS = int(os.getenv("FX_INGEST_INTERVAL_SECONDS", "900"))  # 15 min
HTTP_TIMEOUT_SECONDS = float(os.getenv("FX_HTTP_TIMEOUT_SECONDS", "15"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(value):
    """
    Accepts unix seconds or an ISO-8601 string and returns a UTC datetime.
    Falls back to 'now' when a feed omits the field.
    """
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, datetime):
        return ensure_utc(value)
    try:
        return ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        log.warning("Unparseable feed timestamp %r — using current time.", value)
        return datetime.now(timezone.utc)


def _build_rows(rates_map, timestamp, source):
    """Turns a ``{"NGN": 1750.0}`` mapping into ``(timestamp, pair, rate, source)`` rows."""
    rows = []
    for symbol, rate in (rates_map or {}).items():
        symbol = str(symbol).upper()
        if symbol not in SUPPORTED_CURRENCIES:
            continue
        rows.append((timestamp, f"{BASE_CURRENCY}/{symbol}", rate, source))
    return rows


def validate_rates(rates, now=None, max_age_minutes=None):
    """
    Filters out quotes that must never reach the database.

    A row is dropped when it is stale (the stale-rate guard), timestamped in the
    future beyond tolerated clock skew, priced at a non-positive/non-finite
    value, or quoted on a pair we do not track. Returns the surviving rows.

    ``max_age_minutes`` overrides the freshness budget for every row; by default
    each row is held to its own source's budget (see
    :func:`lib.fx_utils.max_age_for_source`), because a once-a-day feed and a
    15-minute feed cannot share one rule.
    """
    kept = []
    for row in rates:
        try:
            timestamp, pair, rate, source = row
        except (TypeError, ValueError):
            log.warning("Discarding malformed rate row: %r", row)
            continue

        if not isinstance(timestamp, datetime):
            log.warning(
                "Discarding %s rate with non-datetime timestamp: %r", pair, timestamp
            )
            continue

        if pair not in TRACKED_PAIRS:
            log.warning("Discarding rate for untracked pair %r from %s.", pair, source)
            continue

        if not is_valid_rate(rate):
            log.warning(
                "Discarding non-positive %s rate %r from %s.", pair, rate, source
            )
            continue

        budget = (
            max_age_for_source(source) if max_age_minutes is None else max_age_minutes
        )
        if not is_fresh(timestamp, now=now, max_age_minutes=budget):
            log.warning(
                "Stale-rate guard: dropping %s from %s quoted at %s (max age %d min).",
                pair,
                source,
                timestamp,
                budget,
            )
            continue

        kept.append((ensure_utc(timestamp), pair, float(rate), source))
    return kept


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


async def fetch_fixer_rates():
    """Fetches official rates from Fixer.io."""
    if not FIXER_API_KEY:
        log.warning("Fixer.io API key not found.")
        return []

    url = (
        "http://data.fixer.io/api/latest"
        f"?access_key={FIXER_API_KEY}&symbols={','.join(CURRENCIES)}"
    )
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

        if not data.get("success"):
            log.error("Fixer.io API error: %s", data.get("error"))
            return []

        timestamp = _parse_timestamp(data.get("timestamp"))
        return _build_rows(data.get("rates"), timestamp, "Fixer.io")
    except Exception as e:  # noqa: BLE001
        log.error("Error fetching from Fixer.io: %s", e)
        return []


async def fetch_open_exchange_rates():
    """Fetches official rates from Open Exchange Rates."""
    if not OPEN_EXCHANGE_RATES_APP_ID:
        log.warning("Open Exchange Rates App ID not found.")
        return []

    url = (
        "https://openexchangerates.org/api/latest.json"
        f"?app_id={OPEN_EXCHANGE_RATES_APP_ID}&symbols={','.join(CURRENCIES)}"
    )
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()

        if data.get("error"):
            log.error("Open Exchange Rates API error: %s", data.get("description"))
            return []

        timestamp = _parse_timestamp(data.get("timestamp"))
        return _build_rows(data.get("rates"), timestamp, "OpenExchangeRates")
    except Exception as e:  # noqa: BLE001
        log.error("Error fetching from Open Exchange Rates: %s", e)
        return []


async def fetch_exchangerate_api_rates():
    """
    Fetches official rates from ExchangeRate-API's keyless open endpoint.

    This is the last-resort fallback: it needs no credentials, so a deployment
    without vendor keys still serves real NGN/KES rates rather than nothing. The
    free tier refreshes once a day, which is why it carries its own freshness
    budget in :data:`lib.fx_utils.SOURCE_MAX_AGE_MINUTES` — it keeps
    ``GET /fx/current`` honest, but it is too coarse to compute volatility from.
    """
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(EXCHANGERATE_API_URL)
            response.raise_for_status()
            data = response.json()

        if data.get("result") != "success":
            log.error("ExchangeRate-API error: %s", data.get("error-type", data))
            return []

        timestamp = _parse_timestamp(data.get("time_last_update_unix"))
        return _build_rows(data.get("rates"), timestamp, "ExchangeRate-API")
    except Exception as e:  # noqa: BLE001
        log.error("Error fetching from ExchangeRate-API: %s", e)
        return []


# Official vendors, keyed by the value accepted in FX_PRIMARY_SOURCE.
OFFICIAL_VENDORS = ("fixer", "openexchangerates", "exchangerate_api")


def _official_fetchers():
    """
    Returns ``[(source_name, fetcher), ...]`` with the primary vendor first and
    every other vendor after it as fallback.

    The mapping is built per call so the fetchers are resolved from the module
    namespace at call time (which keeps them patchable in tests).
    """
    vendors = {
        "fixer": ("Fixer.io", fetch_fixer_rates),
        "openexchangerates": ("OpenExchangeRates", fetch_open_exchange_rates),
        "exchangerate_api": ("ExchangeRate-API", fetch_exchangerate_api_rates),
    }
    primary = FX_PRIMARY_SOURCE if FX_PRIMARY_SOURCE in vendors else "fixer"
    order = [primary] + [key for key in OFFICIAL_VENDORS if key != primary]
    return [vendors[key] for key in order]


async def fetch_official_rates(now=None):
    """
    Fetches official rates from the primary vendor, failing over to the next
    vendor when the primary returns nothing usable.

    "Nothing usable" covers a missing API key, an HTTP/API error and — just as
    importantly — a response whose quotes are all stale, since a frozen feed is
    indistinguishable from a dead one for our purposes.

    Returns ``(rows, source_name)``; ``source_name`` is None when every vendor
    failed.
    """
    for name, fetcher in _official_fetchers():
        rows = validate_rates(await fetcher(), now=now)
        if rows:
            log.info("Fetched %d official rate(s) from %s.", len(rows), name)
            return rows, name
        log.warning("Official source %s returned no usable rates — failing over.", name)

    log.error("All official FX sources failed. No official rates this cycle.")
    return [], None


async def fetch_parallel_market_rates(now=None):
    """
    Fetches parallel-market ("street") rates — the rates NGN/KES holders
    actually transact at, which can diverge from the official rate by 20%+.

    The feed is configured with ``PARALLEL_MARKET_API_URL`` and must return
    ``{"rates": {"NGN": 1750.0, ...}}`` with an optional ``timestamp``.
    """
    if not PARALLEL_MARKET_API_URL:
        log.warning(
            "PARALLEL_MARKET_API_URL is not configured — "
            "skipping parallel-market ingestion this cycle."
        )
        return []

    headers = {}
    if PARALLEL_MARKET_API_KEY:
        headers["Authorization"] = f"Bearer {PARALLEL_MARKET_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                PARALLEL_MARKET_API_URL,
                headers=headers,
                params={"symbols": ",".join(CURRENCIES)},
            )
            response.raise_for_status()
            data = response.json()

        timestamp = _parse_timestamp(data.get("timestamp"))
        rows = _build_rows(data.get("rates"), timestamp, PARALLEL_MARKET_SOURCE)
        rows = validate_rates(rows, now=now)
        log.info(
            "Fetched %d parallel-market rate(s) from %s.",
            len(rows),
            PARALLEL_MARKET_SOURCE,
        )
        return rows
    except Exception as e:  # noqa: BLE001
        log.error("Error fetching parallel-market rates: %s", e)
        return []


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_ingestion(now=None):
    """
    Runs a single ingestion cycle and returns a summary of what was stored::

        {"saved": 6, "official_source": "Fixer.io", "parallel_source": "ParallelMarket",
         "pairs": ["USD/NGN", "USD/KES", ...]}

    Official and parallel feeds are fetched concurrently and stored
    independently, so one going dark never blocks the other.
    """
    started_at = now or datetime.now(timezone.utc)
    log.info("Starting FX ingestion cycle at %s", started_at)

    official, parallel = await asyncio.gather(
        fetch_official_rates(now=started_at),
        fetch_parallel_market_rates(now=started_at),
    )
    official_rows, official_source = official

    all_rates = official_rows + parallel
    summary = {
        "saved": 0,
        "official_source": official_source,
        "parallel_source": PARALLEL_MARKET_SOURCE if parallel else None,
        "pairs": sorted({row[1] for row in all_rates}),
    }

    if not all_rates:
        log.error("No usable rates fetched this cycle — nothing written to fx_rates.")
        return summary

    log.info("Saving %d validated rate(s) to fx_rates...", len(all_rates))
    save_fx_rates(all_rates)
    summary["saved"] = len(all_rates)
    log.info("Ingestion successful: %s", summary)
    return summary


async def run_scheduler(interval_seconds=None, iterations=None):
    """
    Runs :func:`run_ingestion` on a fixed schedule (every 15 minutes by default).

    A failing cycle is logged and never kills the loop — the next cycle retries.
    ``iterations`` bounds the number of cycles (used by the tests); ``None``
    runs forever.
    """
    if interval_seconds is None:
        interval_seconds = INGEST_INTERVAL_SECONDS
    completed = 0

    while iterations is None or completed < iterations:
        try:
            await run_ingestion()
        except Exception:
            log.exception("FX ingestion cycle failed — retrying next cycle.")

        completed += 1
        if iterations is not None and completed >= iterations:
            break
        await asyncio.sleep(interval_seconds)

    return completed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Live FX rate ingester (NGN, KES).")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single ingestion cycle and exit (for cron or manual runs).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=INGEST_INTERVAL_SECONDS,
        help="Seconds between scheduled cycles (default: %(default)s).",
    )
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be a positive number of seconds.")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.once:
        asyncio.run(run_ingestion())
    else:
        asyncio.run(run_scheduler(interval_seconds=args.interval))


if __name__ == "__main__":
    main()
