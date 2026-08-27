"""Tests for the BK-11 key-management endpoints in api/keeper.py."""

import os
import sys

from fastapi.testclient import TestClient

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from api.main import app

client = TestClient(app)

TOKEN = "test-admin-token"


def _auth(**extra):
    return {"x-admin-token": TOKEN, **extra}


# ---------------------------------------------------------------------------
# POST /keeper/emergency-revocate
# ---------------------------------------------------------------------------


def test_emergency_revocate_requires_admin_token(monkeypatch):
    monkeypatch.setenv("KEEPER_ADMIN_TOKEN", TOKEN)
    monkeypatch.setattr(
        "api.keeper.revoke_active_signing_key", lambda *a, **k: {"id": 1}
    )

    assert (
        client.post("/keeper/emergency-revocate", json={"reason": "x"}).status_code
        == 422
    )
    resp = client.post(
        "/keeper/emergency-revocate",
        headers={"x-admin-token": "wrong"},
        json={"reason": "x"},
    )
    assert resp.status_code == 403


def test_emergency_revocate_success(monkeypatch):
    monkeypatch.setenv("KEEPER_ADMIN_TOKEN", TOKEN)
    monkeypatch.delenv("KEY_ACCESS_IP_ALLOWLIST", raising=False)
    captured = {}

    def fake_revoke(reason, actor):
        captured["reason"] = reason
        captured["actor"] = actor
        return {"id": 3, "key_id": "alias/aegis-keeper-2026Q3", "revoked": True}

    monkeypatch.setattr("api.keeper.revoke_active_signing_key", fake_revoke)

    resp = client.post(
        "/keeper/emergency-revocate", headers=_auth(), json={"reason": "seed leak"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "revoked"
    assert body["key"]["key_id"] == "alias/aegis-keeper-2026Q3"
    assert captured["reason"] == "seed leak"
    assert captured["actor"] == "emergency-endpoint"


def test_emergency_revocate_404_when_no_active_key(monkeypatch):
    monkeypatch.setenv("KEEPER_ADMIN_TOKEN", TOKEN)
    monkeypatch.delenv("KEY_ACCESS_IP_ALLOWLIST", raising=False)
    monkeypatch.setattr("api.keeper.revoke_active_signing_key", lambda *a, **k: None)

    resp = client.post(
        "/keeper/emergency-revocate", headers=_auth(), json={"reason": "x"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# IP allowlisting
# ---------------------------------------------------------------------------


def test_ip_allowlist_blocks_ip_outside_range(monkeypatch):
    monkeypatch.setenv("KEEPER_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("KEY_ACCESS_IP_ALLOWLIST", "10.0.0.0/8, 192.168.1.5")
    monkeypatch.setenv("KEY_ACCESS_TRUST_FORWARDED_FOR", "true")
    monkeypatch.setattr(
        "api.keeper.revoke_active_signing_key", lambda *a, **k: {"id": 1}
    )

    blocked = client.post(
        "/keeper/emergency-revocate",
        headers=_auth(**{"x-forwarded-for": "1.2.3.4"}),
        json={"reason": "x"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "IP not allowlisted for key access"

    allowed = client.post(
        "/keeper/emergency-revocate",
        headers=_auth(**{"x-forwarded-for": "10.9.9.9"}),
        json={"reason": "x"},
    )
    assert allowed.status_code == 200


def test_ip_allowlist_rejects_unparseable_peer(monkeypatch):
    # allowlist set but X-Forwarded-For not trusted → peer host is "testclient"
    monkeypatch.setenv("KEEPER_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("KEY_ACCESS_IP_ALLOWLIST", "10.0.0.0/8")
    monkeypatch.delenv("KEY_ACCESS_TRUST_FORWARDED_FOR", raising=False)
    monkeypatch.setattr("api.keeper.rotate_signing_key", lambda **k: {"rotated": False})

    resp = client.post("/keeper/rotate-key", headers=_auth())
    assert resp.status_code == 403


def test_ip_allowlist_disabled_when_unset(monkeypatch):
    monkeypatch.setenv("KEEPER_ADMIN_TOKEN", TOKEN)
    monkeypatch.delenv("KEY_ACCESS_IP_ALLOWLIST", raising=False)
    monkeypatch.setattr("api.keeper.rotate_signing_key", lambda **k: {"rotated": True})

    resp = client.post("/keeper/rotate-key", headers=_auth())
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /keeper/rotate-key  &  GET /keeper/signing-audit
# ---------------------------------------------------------------------------


def test_rotate_key_delegates_to_key_manager(monkeypatch):
    monkeypatch.setenv("KEEPER_ADMIN_TOKEN", TOKEN)
    monkeypatch.delenv("KEY_ACCESS_IP_ALLOWLIST", raising=False)
    monkeypatch.setattr(
        "api.keeper.rotate_signing_key",
        lambda actor: {
            "rotated": True,
            "key_id": "alias/aegis-keeper-2026Q3",
            "actor": actor,
        },
    )
    resp = client.post("/keeper/rotate-key", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["rotated"] is True
    assert resp.json()["actor"] == "rotate-endpoint"


def test_signing_audit_requires_token_and_returns_rows(monkeypatch):
    monkeypatch.setenv("KEEPER_ADMIN_TOKEN", TOKEN)
    rows = [
        {"id": 2, "tx_hash": "b", "key_id": "alias/x", "actor": "keeper_bot"},
        {"id": 1, "tx_hash": "a", "key_id": "alias/x", "actor": "keeper_bot"},
    ]
    monkeypatch.setattr("api.keeper.get_signing_audit_log", lambda limit: rows[:limit])

    assert client.get("/keeper/signing-audit").status_code == 422

    resp = client.get("/keeper/signing-audit?limit=2", headers=_auth())
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()] == [2, 1]
