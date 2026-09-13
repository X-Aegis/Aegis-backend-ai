import base64
import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from api.main import app


def _token(secret: str, expires_at: int) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps({"partnerId": "partner-primary", "expiresAt": expires_at}).encode()
    ).decode().rstrip("=")
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def test_partner_login_and_session(monkeypatch):
    monkeypatch.setenv("AEGIS_PARTNER_EMAIL", "partner@example.com")
    monkeypatch.setenv("AEGIS_PARTNER_PASSWORD", "secret")
    monkeypatch.setenv("AEGIS_PARTNER_SECRET", "signing-secret")
    client = TestClient(app)

    response = client.post("/partner/login", json={"email": "partner@example.com", "password": "secret"})
    assert response.status_code == 200
    token = response.json()["token"]
    assert client.get("/partner/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_partner_rejects_invalid_or_expired_sessions(monkeypatch):
    monkeypatch.setenv("AEGIS_PARTNER_EMAIL", "partner@example.com")
    monkeypatch.setenv("AEGIS_PARTNER_PASSWORD", "secret")
    monkeypatch.setenv("AEGIS_PARTNER_SECRET", "signing-secret")
    client = TestClient(app)

    assert client.post("/partner/login", json={"email": "partner@example.com", "password": "wrong"}).status_code == 401
    expired = _token("signing-secret", int(time.time()) - 1)
    assert client.get("/partner/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
