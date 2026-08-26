"""Tests for the live FX ingester: source fallback, freshness validation and
the stale-rate guard."""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import forex_ingester
from services.forex_ingester import (
    _build_rows,
    _parse_timestamp,
    fetch_official_rates,
    fetch_parallel_market_rates,
    run_ingestion,
    run_scheduler,
    validate_rates,
)

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _row(pair="USD/NGN", rate=1750.0, source="Fixer.io", age_minutes=0):
    return (NOW - timedelta(minutes=age_minutes), pair, rate, source)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in returning a canned response."""

    def __init__(self, response):
        self._response = response

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, *args, **kwargs):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


# ---------------------------------------------------------------------------
# Freshness validation / stale-rate guard
# ---------------------------------------------------------------------------


def test_validate_rates_keeps_fresh_quotes():
    kept = validate_rates([_row(age_minutes=2)], now=NOW)

    assert len(kept) == 1
    timestamp, pair, rate, source = kept[0]
    assert pair == "USD/NGN"
    assert rate == 1750.0
    assert source == "Fixer.io"
    assert timestamp.tzinfo is not None


def test_validate_rates_drops_stale_quotes():
    """A quote older than the max age never reaches the database."""
    fresh = _row(pair="USD/KES", age_minutes=5)
    stale = _row(pair="USD/NGN", age_minutes=120)

    kept = validate_rates([fresh, stale], now=NOW)

    assert [row[1] for row in kept] == ["USD/KES"]


def test_validate_rates_honours_custom_max_age():
    rows = [_row(age_minutes=30)]

    assert validate_rates(rows, now=NOW, max_age_minutes=45) == validate_rates(
        rows, now=NOW
    )
    assert validate_rates(rows, now=NOW, max_age_minutes=15) == []


def test_validate_rates_drops_quotes_from_the_future():
    """A source with a broken clock is not a source we can price off."""
    future = (NOW + timedelta(hours=2), "USD/NGN", 1750.0, "Fixer.io")

    assert validate_rates([future], now=NOW) == []


def test_validate_rates_tolerates_small_clock_skew():
    slightly_ahead = (NOW + timedelta(minutes=2), "USD/NGN", 1750.0, "Fixer.io")

    assert len(validate_rates([slightly_ahead], now=NOW)) == 1


@pytest.mark.parametrize("bad_rate", [0, -1750.0, None, "not-a-number", float("nan")])
def test_validate_rates_drops_non_positive_or_unparseable_rates(bad_rate):
    assert validate_rates([_row(rate=bad_rate)], now=NOW) == []


def test_validate_rates_drops_untracked_pairs():
    assert validate_rates([_row(pair="USD/JPY")], now=NOW) == []


def test_validate_rates_drops_malformed_rows():
    assert validate_rates([("only", "three")], now=NOW) == []


def test_validate_rates_drops_non_datetime_timestamps():
    assert (
        validate_rates([("2026-06-01", "USD/NGN", 1750.0, "Fixer.io")], now=NOW) == []
    )


def test_validate_rates_normalises_naive_timestamps_to_utc():
    naive = (NOW.replace(tzinfo=None), "USD/NGN", 1750.0, "Fixer.io")

    kept = validate_rates([naive], now=NOW)

    assert kept[0][0] == NOW


def test_validate_rates_gives_a_daily_feed_its_own_freshness_budget():
    """A once-a-day vendor must not be judged by the 15-minute feeds' rule."""
    daily = _row(source="ExchangeRate-API", age_minutes=600)
    intraday = _row(source="Fixer.io", age_minutes=600)

    kept = validate_rates([daily, intraday], now=NOW)

    assert [row[3] for row in kept] == ["ExchangeRate-API"]


def test_validate_rates_still_drops_a_daily_feed_past_its_budget():
    ancient = _row(source="ExchangeRate-API", age_minutes=3 * 24 * 60)

    assert validate_rates([ancient], now=NOW) == []


# ---------------------------------------------------------------------------
# Source fallback
# ---------------------------------------------------------------------------


def test_official_fallback_used_when_primary_is_down(monkeypatch):
    """Primary vendor down (no rows) → the secondary vendor's rates are used."""

    async def dead_primary():
        return []

    async def healthy_fallback():
        return [_row(source="OpenExchangeRates")]

    monkeypatch.setattr(forex_ingester, "fetch_fixer_rates", dead_primary)
    monkeypatch.setattr(forex_ingester, "fetch_open_exchange_rates", healthy_fallback)

    rows, source = asyncio.run(fetch_official_rates(now=NOW))

    assert source == "OpenExchangeRates"
    assert [row[3] for row in rows] == ["OpenExchangeRates"]


def test_official_fallback_used_when_primary_is_stale(monkeypatch):
    """A frozen primary feed is treated exactly like a dead one."""

    async def stale_primary():
        return [_row(source="Fixer.io", age_minutes=180)]

    async def healthy_fallback():
        return [_row(source="OpenExchangeRates")]

    monkeypatch.setattr(forex_ingester, "fetch_fixer_rates", stale_primary)
    monkeypatch.setattr(forex_ingester, "fetch_open_exchange_rates", healthy_fallback)

    rows, source = asyncio.run(fetch_official_rates(now=NOW))

    assert source == "OpenExchangeRates"
    assert len(rows) == 1


def test_primary_is_preferred_when_healthy(monkeypatch):
    async def healthy_primary():
        return [_row(source="Fixer.io")]

    async def healthy_fallback():
        raise AssertionError("fallback must not be called when the primary is healthy")

    monkeypatch.setattr(forex_ingester, "fetch_fixer_rates", healthy_primary)
    monkeypatch.setattr(forex_ingester, "fetch_open_exchange_rates", healthy_fallback)

    _, source = asyncio.run(fetch_official_rates(now=NOW))

    assert source == "Fixer.io"


def test_primary_source_is_configurable(monkeypatch):
    async def fixer():
        return [_row(source="Fixer.io")]

    async def oxr():
        return [_row(source="OpenExchangeRates")]

    monkeypatch.setattr(forex_ingester, "FX_PRIMARY_SOURCE", "openexchangerates")
    monkeypatch.setattr(forex_ingester, "fetch_fixer_rates", fixer)
    monkeypatch.setattr(forex_ingester, "fetch_open_exchange_rates", oxr)

    _, source = asyncio.run(fetch_official_rates(now=NOW))

    assert source == "OpenExchangeRates"


def test_official_rates_empty_when_every_vendor_fails(monkeypatch):
    async def down():
        return []

    monkeypatch.setattr(forex_ingester, "fetch_fixer_rates", down)
    monkeypatch.setattr(forex_ingester, "fetch_open_exchange_rates", down)
    monkeypatch.setattr(forex_ingester, "fetch_exchangerate_api_rates", down)

    rows, source = asyncio.run(fetch_official_rates(now=NOW))

    assert rows == []
    assert source is None


def test_fixer_returns_empty_without_api_key(monkeypatch):
    monkeypatch.setattr(forex_ingester, "FIXER_API_KEY", None)

    assert asyncio.run(forex_ingester.fetch_fixer_rates()) == []


def test_open_exchange_rates_returns_empty_without_app_id(monkeypatch):
    monkeypatch.setattr(forex_ingester, "OPEN_EXCHANGE_RATES_APP_ID", None)

    assert asyncio.run(forex_ingester.fetch_open_exchange_rates()) == []


def test_fixer_returns_empty_on_api_error(monkeypatch):
    monkeypatch.setattr(forex_ingester, "FIXER_API_KEY", "test-key")
    monkeypatch.setattr(
        forex_ingester.httpx,
        "AsyncClient",
        _FakeAsyncClient(_FakeResponse({"success": False, "error": {"code": 104}})),
    )

    assert asyncio.run(forex_ingester.fetch_fixer_rates()) == []


def test_fixer_parses_a_successful_response(monkeypatch):
    monkeypatch.setattr(forex_ingester, "FIXER_API_KEY", "test-key")
    monkeypatch.setattr(
        forex_ingester.httpx,
        "AsyncClient",
        _FakeAsyncClient(
            _FakeResponse(
                {
                    "success": True,
                    "timestamp": int(NOW.timestamp()),
                    "rates": {"NGN": 1750.25, "KES": 132.4, "JPY": 157.0},
                }
            )
        ),
    )

    rows = asyncio.run(forex_ingester.fetch_fixer_rates())

    pairs = {row[1]: row[2] for row in rows}
    assert pairs == {"USD/NGN": 1750.25, "USD/KES": 132.4}  # JPY is not tracked
    assert {row[3] for row in rows} == {"Fixer.io"}


def test_exchangerate_api_is_the_last_resort_fallback(monkeypatch):
    """With no vendor keys configured, the keyless feed still yields rates."""

    async def no_key():
        return []

    async def keyless():
        return [_row(source="ExchangeRate-API", age_minutes=300)]

    monkeypatch.setattr(forex_ingester, "fetch_fixer_rates", no_key)
    monkeypatch.setattr(forex_ingester, "fetch_open_exchange_rates", no_key)
    monkeypatch.setattr(forex_ingester, "fetch_exchangerate_api_rates", keyless)

    rows, source = asyncio.run(fetch_official_rates(now=NOW))

    assert source == "ExchangeRate-API"
    assert len(rows) == 1


def test_exchangerate_api_parses_the_open_endpoint_response(monkeypatch):
    monkeypatch.setattr(
        forex_ingester.httpx,
        "AsyncClient",
        _FakeAsyncClient(
            _FakeResponse(
                {
                    "result": "success",
                    "base_code": "USD",
                    "time_last_update_unix": int(NOW.timestamp()),
                    "rates": {"NGN": 1749.9, "KES": 129.4, "USD": 1.0},
                }
            )
        ),
    )

    rows = asyncio.run(forex_ingester.fetch_exchangerate_api_rates())

    assert {row[1]: row[2] for row in rows} == {"USD/NGN": 1749.9, "USD/KES": 129.4}
    assert {row[3] for row in rows} == {"ExchangeRate-API"}


def test_exchangerate_api_returns_empty_on_error_payload(monkeypatch):
    monkeypatch.setattr(
        forex_ingester.httpx,
        "AsyncClient",
        _FakeAsyncClient(
            _FakeResponse({"result": "error", "error-type": "unsupported-code"})
        ),
    )

    assert asyncio.run(forex_ingester.fetch_exchangerate_api_rates()) == []


# ---------------------------------------------------------------------------
# Parallel-market feed
# ---------------------------------------------------------------------------


def test_parallel_market_skipped_when_not_configured(monkeypatch):
    monkeypatch.setattr(forex_ingester, "PARALLEL_MARKET_API_URL", None)

    assert asyncio.run(fetch_parallel_market_rates(now=NOW)) == []


def test_parallel_market_rates_are_parsed_and_tagged(monkeypatch):
    monkeypatch.setattr(
        forex_ingester, "PARALLEL_MARKET_API_URL", "https://example.test/rates"
    )
    monkeypatch.setattr(
        forex_ingester.httpx,
        "AsyncClient",
        _FakeAsyncClient(
            _FakeResponse(
                {
                    "base": "USD",
                    "timestamp": NOW.isoformat(),
                    "rates": {"NGN": 1815.0, "KES": 134.9},
                }
            )
        ),
    )

    rows = asyncio.run(fetch_parallel_market_rates(now=NOW))

    assert {row[1] for row in rows} == {"USD/NGN", "USD/KES"}
    assert {row[3] for row in rows} == {forex_ingester.PARALLEL_MARKET_SOURCE}


def test_parallel_market_errors_are_swallowed(monkeypatch):
    """The parallel feed going down must not take official ingestion with it."""
    monkeypatch.setattr(
        forex_ingester, "PARALLEL_MARKET_API_URL", "https://example.test/rates"
    )
    monkeypatch.setattr(
        forex_ingester.httpx,
        "AsyncClient",
        _FakeAsyncClient(RuntimeError("connection refused")),
    )

    assert asyncio.run(fetch_parallel_market_rates(now=NOW)) == []


def test_parallel_market_stale_quotes_are_dropped(monkeypatch):
    monkeypatch.setattr(
        forex_ingester, "PARALLEL_MARKET_API_URL", "https://example.test/rates"
    )
    monkeypatch.setattr(
        forex_ingester.httpx,
        "AsyncClient",
        _FakeAsyncClient(
            _FakeResponse(
                {
                    "timestamp": (NOW - timedelta(hours=6)).isoformat(),
                    "rates": {"NGN": 1815.0},
                }
            )
        ),
    )

    assert asyncio.run(fetch_parallel_market_rates(now=NOW)) == []


# ---------------------------------------------------------------------------
# Ingestion cycle
# ---------------------------------------------------------------------------


def test_run_ingestion_saves_official_and_parallel_rates(monkeypatch):
    saved = {}

    async def official(now=None):
        return [_row(source="Fixer.io")], "Fixer.io"

    async def parallel(now=None):
        return [_row(rate=1815.0, source="ParallelMarket")]

    monkeypatch.setattr(forex_ingester, "fetch_official_rates", official)
    monkeypatch.setattr(forex_ingester, "fetch_parallel_market_rates", parallel)
    monkeypatch.setattr(
        forex_ingester, "save_fx_rates", lambda rates: saved.update(rates=rates)
    )

    summary = asyncio.run(run_ingestion(now=NOW))

    assert summary["saved"] == 2
    assert summary["official_source"] == "Fixer.io"
    assert summary["parallel_source"] == "ParallelMarket"
    assert {row[3] for row in saved["rates"]} == {"Fixer.io", "ParallelMarket"}


def test_run_ingestion_still_saves_when_parallel_feed_is_down(monkeypatch):
    saved = {}

    async def official(now=None):
        return [_row(source="OpenExchangeRates")], "OpenExchangeRates"

    async def parallel(now=None):
        return []

    monkeypatch.setattr(forex_ingester, "fetch_official_rates", official)
    monkeypatch.setattr(forex_ingester, "fetch_parallel_market_rates", parallel)
    monkeypatch.setattr(
        forex_ingester, "save_fx_rates", lambda rates: saved.update(rates=rates)
    )

    summary = asyncio.run(run_ingestion(now=NOW))

    assert summary["saved"] == 1
    assert summary["parallel_source"] is None


def test_run_ingestion_writes_nothing_when_all_sources_fail(monkeypatch):
    async def official(now=None):
        return [], None

    async def parallel(now=None):
        return []

    def explode(rates):
        raise AssertionError("save_fx_rates must not be called with no rates")

    monkeypatch.setattr(forex_ingester, "fetch_official_rates", official)
    monkeypatch.setattr(forex_ingester, "fetch_parallel_market_rates", parallel)
    monkeypatch.setattr(forex_ingester, "save_fx_rates", explode)

    summary = asyncio.run(run_ingestion(now=NOW))

    assert summary["saved"] == 0
    assert summary["official_source"] is None


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def test_scheduler_runs_the_requested_number_of_cycles(monkeypatch):
    cycles = []

    async def fake_ingestion():
        cycles.append(1)
        return {"saved": 1}

    monkeypatch.setattr(forex_ingester, "run_ingestion", fake_ingestion)

    completed = asyncio.run(run_scheduler(interval_seconds=0, iterations=3))

    assert completed == 3
    assert len(cycles) == 3


def test_scheduler_survives_a_failing_cycle(monkeypatch):
    cycles = []

    async def flaky_ingestion():
        cycles.append(1)
        if len(cycles) == 1:
            raise RuntimeError("vendor API exploded")
        return {"saved": 1}

    monkeypatch.setattr(forex_ingester, "run_ingestion", flaky_ingestion)

    completed = asyncio.run(run_scheduler(interval_seconds=0, iterations=2))

    assert completed == 2
    assert len(cycles) == 2


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def test_parse_timestamp_accepts_unix_seconds_iso_and_none():
    assert _parse_timestamp(int(NOW.timestamp())) == NOW
    assert _parse_timestamp(NOW.isoformat()) == NOW
    assert _parse_timestamp("2026-06-01T12:00:00Z") == NOW
    assert _parse_timestamp(None).tzinfo is timezone.utc


def test_parse_timestamp_falls_back_to_now_on_garbage():
    assert _parse_timestamp("not a timestamp").tzinfo is timezone.utc


def test_build_rows_ignores_untracked_symbols():
    rows = _build_rows({"NGN": 1750.0, "JPY": 157.0}, NOW, "Fixer.io")

    assert [row[1] for row in rows] == ["USD/NGN"]
