import os
import sys
from datetime import datetime, timezone

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


# ---------------------------------------------------------------------------
# GET /risk/current
# ---------------------------------------------------------------------------


def test_current_risk_returns_latest_prediction(monkeypatch):
    monkeypatch.setattr(
        "api.risk.get_current_prediction",
        lambda horizon: _prediction_row(55.0, horizon),
    )

    response = client.get("/risk/current")

    assert response.status_code == 200
    data = response.json()
    assert data["volatility_score"] == 55.0
    assert data["horizon"] == 1
    assert data["risk_level"] == "MEDIUM"
    assert "timestamp" in data


def test_current_risk_respects_horizon_query_param(monkeypatch):
    captured = {}

    def fake_get(horizon):
        captured["horizon"] = horizon
        return _prediction_row(horizon=horizon)

    monkeypatch.setattr("api.risk.get_current_prediction", fake_get)

    response = client.get("/risk/current", params={"horizon": 24})

    assert response.status_code == 200
    assert captured["horizon"] == 24
    assert response.json()["horizon"] == 24


def test_current_risk_returns_404_when_no_predictions(monkeypatch):
    monkeypatch.setattr("api.risk.get_current_prediction", lambda horizon: None)

    response = client.get("/risk/current")

    assert response.status_code == 404
    assert "horizon" in response.json()["detail"]


def test_current_risk_low_level_below_40(monkeypatch):
    monkeypatch.setattr(
        "api.risk.get_current_prediction", lambda horizon: _prediction_row(25.0)
    )

    response = client.get("/risk/current")

    assert response.status_code == 200
    assert response.json()["risk_level"] == "LOW"


def test_current_risk_high_level_at_80(monkeypatch):
    monkeypatch.setattr(
        "api.risk.get_current_prediction", lambda horizon: _prediction_row(80.0)
    )

    response = client.get("/risk/current")

    assert response.status_code == 200
    assert response.json()["risk_level"] == "HIGH"


def test_current_risk_rejects_invalid_horizon():
    response = client.get("/risk/current", params={"horizon": 0})

    assert response.status_code == 422


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
