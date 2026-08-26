import os
import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.main import app

client = TestClient(app)


def _series(
    rates, start=datetime(2026, 1, 1, tzinfo=timezone.utc), step=timedelta(hours=1)
):
    return [(start + i * step, rate) for i, rate in enumerate(rates)]


def _request_body(**overrides):
    body = {
        "pair": "USD/NGN",
        "start_date": "2026-01-01",
        "end_date": "2026-01-05",
        "strategy_name": "test-strategy",
        "volatility_window": 5,
        "threshold": 50,
        "initial_capital": 10000,
        "stable_apy": 0,
    }
    body.update(overrides)
    return body


def test_create_backtest_persists_and_returns_report(monkeypatch):
    rows = _series([1500.0 + (i % 4) for i in range(30)])

    monkeypatch.setattr(
        "api.backtest.get_fx_rate_series", lambda pair, start, end: rows
    )

    saved = {}

    def fake_save(**kwargs):
        saved.update(kwargs)
        return {"id": 1, "created_at": datetime(2026, 1, 6, tzinfo=timezone.utc)}

    monkeypatch.setattr("api.backtest.save_backtest_result", fake_save)

    response = client.post("/backtest", json=_request_body())

    assert response.status_code == 201
    data = response.json()
    assert data["id"] == 1
    assert data["pair"] == "USD/NGN"
    assert data["strategy_name"] == "test-strategy"
    assert "return_improvement_pct" in data["comparison"]
    assert saved["strategy_name"] == "test-strategy"
    assert saved["pair"] == "USD/NGN"


def test_create_backtest_returns_404_when_no_rates(monkeypatch):
    monkeypatch.setattr("api.backtest.get_fx_rate_series", lambda pair, start, end: [])

    response = client.post("/backtest", json=_request_body())

    assert response.status_code == 404


def test_create_backtest_returns_400_for_invalid_date_range():
    response = client.post(
        "/backtest", json=_request_body(start_date="2026-01-10", end_date="2026-01-01")
    )
    assert response.status_code == 400


def test_create_backtest_returns_422_for_insufficient_data(monkeypatch):
    rows = _series([1500.0, 1501.0, 1502.0])
    monkeypatch.setattr(
        "api.backtest.get_fx_rate_series", lambda pair, start, end: rows
    )

    response = client.post("/backtest", json=_request_body(volatility_window=10))

    assert response.status_code == 422


def test_create_backtest_rejects_out_of_range_threshold():
    response = client.post("/backtest", json=_request_body(threshold=150))
    assert response.status_code == 422


def test_get_backtest_results_returns_stored_reports(monkeypatch):
    stored_row = {
        "id": 1,
        "created_at": datetime(2026, 1, 6, tzinfo=timezone.utc),
        "strategy_name": "test-strategy",
        "pair": "USD/NGN",
        "start_date": datetime(2026, 1, 1, tzinfo=timezone.utc).date(),
        "end_date": datetime(2026, 1, 5, tzinfo=timezone.utc).date(),
        "data_points_used": 30,
        "params": {
            "volatility_window": 5,
            "threshold": 50,
            "initial_capital": 10000,
            "stable_apy": 0,
        },
        "strategy_metrics": {
            "final_value": 10500.0,
            "total_return_pct": 5.0,
            "max_drawdown_pct": 1.0,
            "sharpe_ratio": 1.5,
            "win_rate_pct": 55.0,
            "num_regime_switches": 2,
            "time_in_stable_pct": 10.0,
        },
        "baseline_metrics": {
            "final_value": 10200.0,
            "total_return_pct": 2.0,
            "max_drawdown_pct": 3.0,
            "sharpe_ratio": 0.8,
            "win_rate_pct": 48.0,
            "num_regime_switches": 0,
            "time_in_stable_pct": 0.0,
        },
        "comparison": {
            "return_improvement_pct": 3.0,
            "drawdown_reduction_pct": 2.0,
            "sharpe_improvement": 0.7,
        },
    }

    captured = {}

    def fake_list(pair=None, strategy_name=None, limit=20, offset=0):
        captured.update(
            pair=pair, strategy_name=strategy_name, limit=limit, offset=offset
        )
        return [stored_row]

    monkeypatch.setattr("api.backtest.list_backtest_results", fake_list)

    response = client.get("/backtest/results", params={"pair": "USD/NGN", "limit": 5})

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["pair"] == "USD/NGN"
    assert captured["pair"] == "USD/NGN"
    assert captured["limit"] == 5


def test_create_backtest_with_gru_model(monkeypatch):
    rows = _series([1500.0 + (i % 4) for i in range(30)])
    monkeypatch.setattr(
        "api.backtest.get_fx_rate_series", lambda pair, start, end: rows
    )

    saved = {}

    def fake_save(**kwargs):
        saved.update(kwargs)
        return {"id": 2, "created_at": datetime(2026, 1, 6, tzinfo=timezone.utc)}

    monkeypatch.setattr("api.backtest.save_backtest_result", fake_save)

    response = client.post("/backtest", json=_request_body(model_type="gru"))

    assert response.status_code == 201
    data = response.json()
    assert data["id"] == 2
    assert data["params"]["model_type"] == "gru"
    assert saved["params"]["model_type"] == "gru"


def test_create_backtest_with_invalid_model_type(monkeypatch):
    rows = _series([1500.0 + (i % 4) for i in range(30)])
    monkeypatch.setattr(
        "api.backtest.get_fx_rate_series", lambda pair, start, end: rows
    )

    response = client.post("/backtest", json=_request_body(model_type="invalid"))

    assert response.status_code == 422
