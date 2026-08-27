"""Tests for FX pair normalisation and rate freshness helpers."""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.fx_utils import (
    MARKET_OFFICIAL,
    MARKET_PARALLEL,
    OFFICIAL_SOURCES,
    UnsupportedPairError,
    canonical_pair,
    convert_rate,
    ensure_utc,
    is_fresh,
    is_valid_rate,
    market_for_source,
    parse_pair,
    rate_age_seconds,
    source_filters,
)

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_canonical_pair_accepts_both_orientations():
    assert canonical_pair("USD/NGN") == ("USD/NGN", False)
    assert canonical_pair("NGN/USD") == ("USD/NGN", True)
    assert canonical_pair("kes/usd") == ("USD/KES", True)
    assert canonical_pair("  USD/KES  ") == ("USD/KES", False)


@pytest.mark.parametrize("pair", ["NGN", "USD-NGN", "USD/JPY", "EUR/GBP", "", None, 42])
def test_canonical_pair_rejects_unsupported_input(pair):
    with pytest.raises(UnsupportedPairError):
        canonical_pair(pair)


def test_parse_pair_splits_currencies():
    assert parse_pair("usd/ngn") == ("USD", "NGN")


def test_convert_rate_inverts_only_when_asked():
    assert convert_rate(1750.0, inverted=False) == 1750.0
    assert convert_rate(1750.0, inverted=True) == pytest.approx(1 / 1750.0)


def test_convert_rate_refuses_to_invert_non_positive_rates():
    with pytest.raises(ValueError):
        convert_rate(0, inverted=True)


def test_market_classification():
    for source in OFFICIAL_SOURCES:
        assert market_for_source(source) == MARKET_OFFICIAL
    assert market_for_source("ParallelMarket") == MARKET_PARALLEL
    assert market_for_source("AbokiFX") == MARKET_PARALLEL


def test_source_filters_per_market():
    assert source_filters("official") == (list(OFFICIAL_SOURCES), None)
    assert source_filters("parallel") == (None, list(OFFICIAL_SOURCES))
    assert source_filters("any") == (None, None)


def test_is_fresh_accepts_recent_quotes_and_rejects_stale_ones():
    assert is_fresh(NOW - timedelta(minutes=10), now=NOW)
    assert not is_fresh(NOW - timedelta(minutes=90), now=NOW)


def test_is_fresh_rejects_quotes_far_in_the_future():
    assert not is_fresh(NOW + timedelta(hours=1), now=NOW)
    assert is_fresh(NOW + timedelta(minutes=1), now=NOW)


def test_rate_age_seconds_and_ensure_utc():
    assert rate_age_seconds(NOW - timedelta(minutes=5), now=NOW) == 300
    assert ensure_utc(NOW.replace(tzinfo=None)) == NOW


@pytest.mark.parametrize(
    "rate,expected",
    [(1750.0, True), ("1750", True), (0, False), (-1, False), (None, False)],
)
def test_is_valid_rate(rate, expected):
    assert is_valid_rate(rate) is expected
