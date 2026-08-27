"""Tests for the service health endpoint (BK-15a / issue #36)."""

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_health_endpoint_shape():
    """GET /health returns the service + checks structure."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "x-aegis-backend"
    assert data["status"] in ("ok", "degraded")
    for dep in ("database", "redis", "stellar_rpc"):
        assert dep in data["checks"]
        assert data["checks"][dep]["status"] in ("ok", "error", "not_configured")


def test_health_reports_degraded_when_dependency_fails(monkeypatch):
    """A failing dependency marks the overall health degraded."""
    monkeypatch.setattr("api.health._check_database", lambda: {"status": "ok"})
    monkeypatch.setattr("api.health._check_redis", lambda: {"status": "ok"})
    monkeypatch.setattr(
        "api.health._check_stellar_rpc", lambda: {"status": "error", "detail": "boom"}
    )

    data = client.get("/health").json()
    assert data["status"] == "degraded"
    assert data["checks"]["stellar_rpc"]["status"] == "error"


def test_health_ok_when_all_checks_pass(monkeypatch):
    """All dependencies healthy means overall status ok."""
    monkeypatch.setattr("api.health._check_database", lambda: {"status": "ok"})
    monkeypatch.setattr("api.health._check_redis", lambda: {"status": "ok"})
    monkeypatch.setattr("api.health._check_stellar_rpc", lambda: {"status": "ok"})

    data = client.get("/health").json()
    assert data["status"] == "ok"


def test_health_redis_not_configured(monkeypatch):
    """Redis reports not_configured when REDIS_URL is unset."""
    monkeypatch.setattr("api.health._check_database", lambda: {"status": "ok"})
    monkeypatch.setattr("api.health._check_redis", lambda: {"status": "not_configured"})
    monkeypatch.setattr("api.health._check_stellar_rpc", lambda: {"status": "ok"})

    data = client.get("/health").json()
    assert data["checks"]["redis"]["status"] == "not_configured"
