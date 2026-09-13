"""Environment-backed partner session endpoints.

Partner credentials are deliberately kept out of the frontend bundle. Production
deployments should provide all three settings through the platform secret store.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/partner", tags=["partner"])


class PartnerLoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=256)


class PartnerIdentity(BaseModel):
    id: str
    name: str
    organization: str
    role: str
    permissions: list[str]


class PartnerLoginResponse(BaseModel):
    token: str
    expiresAt: int
    partner: PartnerIdentity


def _settings() -> tuple[str, str, str]:
    email = os.environ.get("AEGIS_PARTNER_EMAIL", "").strip()
    password = os.environ.get("AEGIS_PARTNER_PASSWORD", "")
    secret = os.environ.get("AEGIS_PARTNER_SECRET", "")
    if not email or not password or not secret:
        raise HTTPException(status_code=503, detail="Partner authentication is not configured")
    return email, password, secret


def _encode(payload: dict[str, object], secret: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _decode(token: str, secret: str) -> dict[str, object]:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(decoded)
        if int(payload["expiresAt"]) < int(time.time()):
            raise ValueError
        return payload
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired partner session") from exc


@router.post("/login", response_model=PartnerLoginResponse)
def login(body: PartnerLoginRequest):
    email, password, secret = _settings()
    if not hmac.compare_digest(body.email, email) or not hmac.compare_digest(body.password, password):
        raise HTTPException(status_code=401, detail="Invalid partner credentials")

    expires_at = int(time.time()) + 8 * 60 * 60
    partner = PartnerIdentity(
        id="partner-primary",
        name="Configured Partner",
        organization="Configured Organization",
        role="admin",
        permissions=["view_metrics", "view_analytics"],
    )
    token = _encode({"partnerId": partner.id, "email": email, "expiresAt": expires_at}, secret)
    return PartnerLoginResponse(token=token, expiresAt=expires_at, partner=partner)


@router.get("/me", response_model=PartnerIdentity)
def current_partner(authorization: str | None = Header(default=None)):
    _, _, secret = _settings()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Partner session required")
    _decode(authorization.removeprefix("Bearer "), secret)
    return PartnerIdentity(
        id="partner-primary",
        name="Configured Partner",
        organization="Configured Organization",
        role="admin",
        permissions=["view_metrics", "view_analytics"],
    )
