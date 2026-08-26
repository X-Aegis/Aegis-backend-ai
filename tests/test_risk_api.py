"""Tests for GET /risk/current (live-rate scoring) and GET /risk/history."""

import os
import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.main import app

client = TestClient(app)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _prediction_row(volatility_score=55.0, horizon=1, timestamp=_TS):
    return {
        "timestamp": timestamp,
        "horizon": horizon,
        "volatility_score": volatility_score,
    }


def _rate_rows(rates, source="Fixer.io", pair="USD/NGN", step_minutes=15, end=None):
    """Builds live fx_rates rows ending 'now', one every `step_minutes`."""
    end = end or datetime.now(timezone.utc) - timedelta(minutes=1)
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


def _calm(n=16, start=1750.0, drift=0.25):
    return [start + drift * i for i in range(n)]


def _volatile(n=16, start=1750.0):
    return [start * (1 + (0.03 if i % 2 else -0.03)) for i in range(n)]


def _patch_window(monkeypatch, rows, captured=None):
    def fake_window(pair, since, sources=None, exclude_sources=None, limit=None):
        if captured is not None:
            captured.update(
                pair=pair,
                since=since,
                sources=sources,
                exclude_sources=exclude_sources,
                limit=limit,
            )
        return rows

    monkeypatch.setattr("api.risk.get_fx_rate_window", fake_window)


# ---------------------------------------------------------------------------
# GET /risk/current — computed from live FX rate history
# ---------------------------------------------------------------------------


def test_current_risk_is_computed_from_live_rates(monkeypatch):
    _patch_window(monkeypatch, _rate_rows(_calm()))

    response = client.get("/risk/current")

    assert response.status_code == 200
    data = response.json()
    assert data["pair"] == "USD/NGN"
    assert data["source"] == "Fixer.io"
    assert data["market"] == "official"
    assert data["data_points"] == 16
    assert data["horizon"] == 1
    assert data["window_hours"] == 24
    assert 0 <= data["volatility_score"] <= 100
    assert data["risk_level"] in {"LOW", "MEDIUM", "HIGH"}
    assert data["latest_rate"] == _calm()[-1]
    assert data["rate_age_seconds"] > 0


def test_current_risk_scores_a_volatile_market_higher(monkeypatch):
    _patch_window(monkeypatch, _rate_rows(_calm()))
    calm_score = client.get("/risk/current").json()["volatility_score"]

    _patch_window(monkeypatch, _rate_rows(_volatile()))
    volatile = client.get("/risk/current").json()

    assert volatile["volatility_score"] > calm_score
    assert volatile["risk_level"] == "HIGH"


def test_current_risk_prefers_the_parallel_market_series(monkeypatch):
    rows = _rate_rows(_calm(), source="OpenExchangeRates") + _rate_rows(
        _calm(start=1815.0), source="ParallelMarket"
    )
    _patch_window(monkeypatch, rows)

    data = client.get("/risk/current").json()

    assert data["source"] == "ParallelMarket"
    assert data["market"] == "parallel"
    assert data["latest_rate"] > 1800


def test_current_risk_respects_horizon_query_param(monkeypatch):
    _patch_window(monkeypatch, _rate_rows(_calm(drift=1.5)))
    short = client.get("/risk/current", params={"horizon": 1}).json()

    _patch_window(monkeypatch, _rate_rows(_calm(drift=1.5)))
    long = client.get("/risk/current", params={"horizon": 24}).json()

    assert short["horizon"] == 1
    assert long["horizon"] == 24
    assert long["volatility_score"] > short["volatility_score"]


def test_current_risk_passes_pair_market_and_window_to_the_query(monkeypatch):
    captured = {}
    _patch_window(monkeypatch, _rate_rows(_calm(), source="ParallelMarket"), captured)

    response = client.get(
        "/risk/current",
        params={"pair": "kes/usd", "market": "parallel", "window_hours": 6},
    )

    assert response.status_code == 200
    assert captured["pair"] == "USD/KES"
    assert captured["exclude_sources"] is not None
    assert response.json()["window_hours"] == 6
    age_hours = (datetime.now(timezone.utc) - captured["since"]).total_seconds() / 3600
    assert 5.9 < age_hours < 6.1


def test_current_risk_returns_404_when_no_live_rates_exist(monkeypatch):
    _patch_window(monkeypatch, [])

    response = client.get("/risk/current")

    assert response.status_code == 404
    assert "No live FX rates" in response.json()["detail"]


def test_current_risk_returns_503_when_history_is_too_short(monkeypatch):
    _patch_window(monkeypatch, _rate_rows(_calm(n=4)))

    response = client.get("/risk/current")

    assert response.status_code == 503
    assert "Insufficient live rate history" in response.json()["detail"]


def test_current_risk_stale_guard_returns_503(monkeypatch):
    """Live history that stopped updating hours ago must not be scored silently."""
    stale_end = datetime.now(timezone.utc) - timedelta(hours=4)
    _patch_window(monkeypatch, _rate_rows(_calm(), end=stale_end))

    response = client.get("/risk/current")

    assert response.status_code == 503
    assert "Stale rate history" in response.json()["detail"]


def test_current_risk_stale_guard_can_be_bypassed_explicitly(monkeypatch):
    stale_end = datetime.now(timezone.utc) - timedelta(hours=4)
    _patch_window(monkeypatch, _rate_rows(_calm(), end=stale_end))

    response = client.get("/risk/current", params={"allow_stale": "true"})

    assert response.status_code == 200
    assert response.json()["rate_age_seconds"] > 3600


def test_current_risk_rejects_unsupported_pair(monkeypatch):
    response = client.get("/risk/current", params={"pair": "EUR/GBP"})

    assert response.status_code == 400


def test_current_risk_rejects_invalid_horizon():
    response = client.get("/risk/current", params={"horizon": 0})

    assert response.status_code == 422


def test_current_risk_risk_levels_track_the_score(monkeypatch):
    _patch_window(monkeypatch, _rate_rows(_calm(drift=0.05)))
    assert client.get("/risk/current").json()["risk_level"] == "LOW"

    _patch_window(monkeypatch, _rate_rows(_volatile()))
    assert client.get("/risk/current").json()["risk_level"] == "HIGH"


# ---------------------------------------------------------------------------
# GET /risk/history
# ---------------------------------------------------------------------------


def test_risk_history_returns_list_of_predictions(monkeypatch):
    rows = [
        _prediction_row(
            70.0, timestamp=datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)
        ),
        _prediction_row(
            55.0, timestamp=datetime(2026, 6, 1, 13, 0, tzinfo=timezone.utc)
        ),
        _prediction_row(
            30.0, timestamp=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        ),
    ]
    monkeypatch.setattr(
        "api.risk.get_prediction_history", lambda horizon, limit, offset: rows
    )

    response = client.get("/risk/history")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3
    assert data[0]["volatility_score"] == 70.0
    assert data[1]["volatility_score"] == 55.0
    assert data[2]["volatility_score"] == 30.0


def test_risk_history_passes_pagination_params(monkeypatch):
    captured = {}

    def fake_history(horizon, limit, offset):
        captured.update(horizon=horizon, limit=limit, offset=offset)
        return []

    monkeypatch.setattr("api.risk.get_prediction_history", fake_history)

    response = client.get(
        "/risk/history", params={"horizon": 6, "limit": 50, "offset": 10}
    )

    assert response.status_code == 200
    assert captured == {"horizon": 6, "limit": 50, "offset": 10}


def test_risk_history_returns_empty_list_when_no_data(monkeypatch):
    monkeypatch.setattr(
        "api.risk.get_prediction_history", lambda horizon, limit, offset: []
    )

    response = client.get("/risk/history")

    assert response.status_code == 200
    assert response.json() == []


def test_risk_history_rejects_limit_above_max(monkeypatch):
    monkeypatch.setattr(
        "api.risk.get_prediction_history", lambda horizon, limit, offset: []
    )

    response = client.get("/risk/history", params={"limit": 9999})

    assert response.status_code == 422


def test_risk_history_rejects_negative_offset(monkeypatch):
    monkeypatch.setattr(
        "api.risk.get_prediction_history", lambda horizon, limit, offset: []
    )

    response = client.get("/risk/history", params={"offset": -1})

    assert response.status_code == 422


def test_risk_history_each_point_has_required_fields(monkeypatch):
    monkeypatch.setattr(
        "api.risk.get_prediction_history",
        lambda horizon, limit, offset: [_prediction_row(62.5)],
    )

    response = client.get("/risk/history")

    assert response.status_code == 200
    point = response.json()[0]
    assert "timestamp" in point
    assert "horizon" in point
    assert "volatility_score" in point
