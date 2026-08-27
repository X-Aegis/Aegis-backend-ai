"""Tests for the Prometheus metrics exporter (BK-15a / issue #36)."""

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_metrics_endpoint_returns_prometheus_format():
    """GET /metrics serves the Prometheus text exposition format."""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_metrics_exports_expected_families():
    """The /metrics payload includes the BK-15a metric families."""
    response = client.get("/metrics")
    body = response.text

    for name in (
        "prediction_mae",
        "prediction_rmse",
        "http_request_duration_seconds",
        "keeper_uptime_percent",
        "keeper_rebalances_last_24h",
        "keeper_rebalance_failures",
    ):
        assert f"# HELP {name}" in body, f"missing {name}"


def test_metrics_populates_prediction_accuracy(monkeypatch):
    """Rolling MAE/RMSE are exported when drift summary is available."""
    from datetime import datetime, timezone

    monkeypatch.setattr(
        "api.metrics.get_drift_summary",
        lambda pair, horizon, limit: [
            {
                "timestamp": datetime.now(timezone.utc),
                "pair": "USD/NGN",
                "horizon": 1,
                "predicted": 10.0,
                "actual": 9.0,
                "abs_error": 1.0,
                "rolling_mae": 0.85,
                "rolling_rmse": 1.2,
            }
        ],
    )

    body = client.get("/metrics").text
    assert "prediction_mae 0.85" in body
    assert "prediction_rmse 1.2" in body


def test_metrics_survives_database_failure(monkeypatch):
    """Exporter still responds when the database is unreachable."""
    monkeypatch.setattr(
        "api.metrics.get_drift_summary",
        lambda **_: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    monkeypatch.setattr("api.metrics.get_keeper_status", lambda: None)
    monkeypatch.setattr("api.metrics.get_keeper_stats", lambda: None)

    response = client.get("/metrics")
    assert response.status_code == 200
