import os
import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.main import app

client = TestClient(app)


def test_keeper_status_ok(monkeypatch):
    def mock_get_keeper_status():
        return {"consecutive_failures": 0, "last_heartbeat": datetime.now(timezone.utc)}

    monkeypatch.setattr("api.keeper.get_keeper_status", mock_get_keeper_status)

    response = client.get("/keeper/status")
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "OK"
    assert data["consecutive_failures"] == 0


def test_keeper_status_tripped(monkeypatch):
    def mock_get_keeper_status():
        return {"consecutive_failures": 3, "last_heartbeat": datetime.now(timezone.utc)}

    monkeypatch.setattr("api.keeper.get_keeper_status", mock_get_keeper_status)

    response = client.get("/keeper/status")
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "TRIPPED"
    assert data["consecutive_failures"] == 3


def test_keeper_status_dead_man(monkeypatch):
    def mock_get_keeper_status():
        return {
            "consecutive_failures": 0,
            "last_heartbeat": datetime.now(timezone.utc) - timedelta(hours=7),
        }

    monkeypatch.setattr("api.keeper.get_keeper_status", mock_get_keeper_status)

    response = client.get("/keeper/status")
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "DEAD_MAN_ACTIVE"
    assert data["consecutive_failures"] == 0


def test_restart_circuit_unauthorized(monkeypatch):
    monkeypatch.setenv("KEEPER_ADMIN_TOKEN", "test-token")
    mock_reset = lambda: None
    monkeypatch.setattr("api.keeper.reset_keeper_circuit", mock_reset)

    # Missing header
    response = client.post("/keeper/restart_circuit")
    assert response.status_code == 422

    # Wrong header
    response = client.post(
        "/keeper/restart_circuit", headers={"x-admin-token": "wrong"}
    )
    assert response.status_code == 403


def test_restart_circuit_authorized(monkeypatch):
    monkeypatch.setenv("KEEPER_ADMIN_TOKEN", "test-token")
    mock_reset = lambda: None
    monkeypatch.setattr("api.keeper.reset_keeper_circuit", mock_reset)

    response = client.post(
        "/keeper/restart_circuit", headers={"x-admin-token": "test-token"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "message": "Circuit reset successfully"}
