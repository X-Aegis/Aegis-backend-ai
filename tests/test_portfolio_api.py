import os
import sys
from datetime import datetime, timezone

from fastapi.testclient import TestClient

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.main import app

client = TestClient(app)
_TS = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _prediction(score=55.0):
    return {"timestamp": _TS, "horizon": 1, "volatility_score": score}


def test_allocation_uses_current_prediction(monkeypatch):
    monkeypatch.setattr("api.portfolio.get_current_prediction", lambda horizon: _prediction(85.0))

    response = client.get("/allocation")

    assert response.status_code == 200
    assert response.json()["stablePercent"] == 100.0
    assert response.json()["riskyPercent"] == 0.0


def test_allocation_returns_404_without_prediction(monkeypatch):
    monkeypatch.setattr("api.portfolio.get_current_prediction", lambda horizon: None)

    response = client.get("/allocation")

    assert response.status_code == 404


def test_insights_include_prediction_and_sentiment(monkeypatch):
    monkeypatch.setattr("api.portfolio.get_current_prediction", lambda horizon: _prediction(82.0))
    monkeypatch.setattr(
        "api.portfolio.get_recent_sentiment",
        lambda limit: [{"timestamp": _TS, "keyword": "Naira", "sentiment_score": -0.5}],
    )

    response = client.post("/insights", json={"horizon": 1, "limit": 10})

    assert response.status_code == 200
    assert len(response.json()) == 2
    assert response.json()[0]["type"] == "WARNING"


def test_simulate_is_deterministic_and_uses_risk_data(monkeypatch):
    monkeypatch.setattr("api.portfolio.get_current_prediction", lambda horizon: _prediction(85.0))

    response = client.post(
        "/simulate",
        json={"fxShockPercent": -20, "volatilityShockPercent": 0, "portfolioValueUsd": 10000},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["withoutAegis"]["projectedValueUsd"] == 8000.0
    assert data["withAegis"]["projectedValueUsd"] == 10000.0
    assert data["strategyShiftPercent"] == 100.0


def test_unavailable_portfolio_datasets_are_explicit(monkeypatch):
    response = client.get("/attribution", params={"period": "1M"})
    assert response.status_code == 404

    response = client.post("/yield-breakdown", json={"period": "1M"})
    assert response.status_code == 404

    response = client.get("/strategy/GABC123")
    assert response.status_code == 404


def test_database_failure_returns_503(monkeypatch):
    def fail(horizon):
        raise RuntimeError("database offline")

    monkeypatch.setattr("api.portfolio.get_current_prediction", fail)

    response = client.get("/allocation")

    assert response.status_code == 503
