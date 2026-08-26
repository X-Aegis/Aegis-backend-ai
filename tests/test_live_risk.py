"""Tests for the live volatility scoring used by GET /risk/current."""

import math
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.live_risk import (
    InsufficientRateHistory,
    compute_live_risk,
    realized_volatility,
    sample_interval_seconds,
    scale_to_horizon,
    select_rate_series,
    volatility_score,
)

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _rows(rates, source="Fixer.io", pair="USD/NGN", step_minutes=15, end=NOW):
    """Builds fx_rates rows ending at `end`, one every `step_minutes`."""
    count = len(rates)
    return [
        {
            "timestamp": end - timedelta(minutes=step_minutes * (count - 1 - i)),
            "pair": pair,
            "rate": rate,
            "source": source,
        }
        for i, rate in enumerate(rates)
    ]


def _steady(n=12, start=1750.0, drift=0.5):
    return [start + drift * i for i in range(n)]


def _choppy(n=12, start=1750.0):
    return [start * (1 + (0.02 if i % 2 else -0.02)) for i in range(n)]


# ---------------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------------


def test_select_rate_series_prefers_the_parallel_market():
    rows = _rows(_steady(12), source="Fixer.io") + _rows(
        _steady(12, start=1815.0), source="ParallelMarket"
    )

    series = select_rate_series(rows)

    assert series["source"] == "ParallelMarket"
    assert series["market"] == "parallel"
    assert len(series["points"]) == 12


def test_select_rate_series_falls_back_to_official_when_parallel_is_thin():
    rows = _rows(_steady(12), source="OpenExchangeRates") + _rows(
        [1815.0, 1816.0], source="ParallelMarket"
    )

    series = select_rate_series(rows)

    assert series["source"] == "OpenExchangeRates"
    assert series["market"] == "official"


def test_select_rate_series_never_mixes_sources():
    """Official and parallel quotes sit ~4% apart; mixing them invents volatility."""
    rows = _rows(_steady(12), source="Fixer.io") + _rows(
        _steady(12, start=1815.0), source="ParallelMarket"
    )

    series = select_rate_series(rows)

    assert {row["source"] for row in rows} == {"Fixer.io", "ParallelMarket"}
    assert all(rate > 1800 for _, rate in series["points"])


def test_select_rate_series_drops_non_positive_rates():
    rows = _rows([1750.0, 0, -5, 1760.0], source="Fixer.io")

    series = select_rate_series(rows, min_points=2)

    assert [rate for _, rate in series["points"]] == [1750.0, 1760.0]


def test_select_rate_series_returns_none_without_usable_rows():
    assert select_rate_series([]) is None
    assert select_rate_series(_rows([0, -1])) is None


def test_select_rate_series_sorts_points_oldest_first():
    rows = list(reversed(_rows(_steady(10))))

    points = select_rate_series(rows)["points"]

    assert points == sorted(points, key=lambda point: point[0])


# ---------------------------------------------------------------------------
# Volatility maths
# ---------------------------------------------------------------------------


def test_sample_interval_is_measured_from_the_data():
    points = [(point["timestamp"], point["rate"]) for point in _rows(_steady(5))]

    assert sample_interval_seconds(points) == 900


def test_realized_volatility_is_zero_for_a_flat_series():
    points = [(NOW + timedelta(minutes=15 * i), 1750.0) for i in range(6)]

    assert realized_volatility(points) == pytest.approx(0.0, abs=1e-12)


def test_realized_volatility_grows_with_choppiness():
    calm = [(point["timestamp"], point["rate"]) for point in _rows(_steady(12))]
    wild = [(point["timestamp"], point["rate"]) for point in _rows(_choppy(12))]

    assert realized_volatility(wild) > realized_volatility(calm)


def test_realized_volatility_needs_at_least_three_points():
    with pytest.raises(InsufficientRateHistory):
        realized_volatility([(NOW, 1750.0), (NOW + timedelta(minutes=15), 1751.0)])


def test_scale_to_horizon_follows_square_root_of_time():
    scaled = scale_to_horizon(0.01, interval_seconds=900, horizon_hours=1)

    assert scaled == pytest.approx(0.01 * math.sqrt(4))


def test_scale_to_horizon_rejects_an_unknown_interval():
    with pytest.raises(InsufficientRateHistory):
        scale_to_horizon(0.01, interval_seconds=None, horizon_hours=1)


def test_volatility_score_is_bounded_and_monotonic():
    assert volatility_score(0.0) == 0.0
    assert 0 < volatility_score(0.005) < volatility_score(0.02) < 100
    assert volatility_score(0.02) == pytest.approx(63.21, abs=0.05)
    assert volatility_score(10.0) == 100.0


def test_volatility_score_crosses_high_around_a_three_percent_move():
    assert volatility_score(0.032) == pytest.approx(79.81, abs=0.05)
    assert volatility_score(0.034) >= 80


# ---------------------------------------------------------------------------
# End-to-end scoring
# ---------------------------------------------------------------------------


def test_compute_live_risk_reports_the_series_it_used():
    rows = _rows(_steady(16), source="ParallelMarket")

    risk = compute_live_risk(rows, horizon=1)

    assert risk["source"] == "ParallelMarket"
    assert risk["market"] == "parallel"
    assert risk["data_points"] == 16
    assert risk["latest_rate"] == rows[-1]["rate"]
    assert risk["latest_timestamp"] == rows[-1]["timestamp"]
    assert risk["sample_interval_seconds"] == 900
    assert 0 <= risk["volatility_score"] <= 100


def test_compute_live_risk_scores_a_calm_market_lower_than_a_volatile_one():
    calm = compute_live_risk(_rows(_steady(16)), horizon=1)
    wild = compute_live_risk(_rows(_choppy(16)), horizon=1)

    assert calm["volatility_score"] < wild["volatility_score"]
    assert wild["volatility_score"] > 80


def test_compute_live_risk_scales_with_the_horizon():
    rows = _rows(_steady(16, drift=2.0))

    short = compute_live_risk(rows, horizon=1)
    long = compute_live_risk(rows, horizon=24)

    assert long["realized_volatility"] > short["realized_volatility"]
    assert long["volatility_score"] > short["volatility_score"]


def test_compute_live_risk_rejects_a_too_short_history():
    with pytest.raises(InsufficientRateHistory):
        compute_live_risk(_rows(_steady(4)), horizon=1)


def test_compute_live_risk_rejects_an_empty_window():
    with pytest.raises(InsufficientRateHistory):
        compute_live_risk([], horizon=1)
