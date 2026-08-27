import ipaddress
import os
import sys
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import (
    get_keeper_stats,
    get_keeper_status,
    get_signing_audit_log,
    reset_keeper_circuit,
    revoke_active_signing_key,
    set_manual_override,
)
from services.key_manager import rotate_signing_key

router = APIRouter(prefix="/keeper", tags=["keeper"])

_TRUTHY = ("1", "true", "yes", "on")


class KeeperStatusResponse(BaseModel):
    state: str
    last_heartbeat: datetime
    consecutive_failures: int


class KeeperOverrideRequest(BaseModel):
    manual_override: bool


class EmergencyRevokeRequest(BaseModel):
    reason: str


@router.get("/status", response_model=KeeperStatusResponse)
def get_status():
    """
    Returns the current state of the keeper bot circuit breaker.
    state can be: OK, TRIPPED, DEAD_MAN_ACTIVE
    """
    status = get_keeper_status()
    if not status:
        raise HTTPException(
            status_code=500, detail="Keeper status not found in database"
        )

    failures = status["consecutive_failures"]
    last_heartbeat = status["last_heartbeat"]

    # Calculate time difference
    now = datetime.now(timezone.utc)
    diff = now - last_heartbeat
    diff_hours = diff.total_seconds() / 3600

    if failures >= 3:
        state = "TRIPPED"
    elif diff_hours >= 6:
        state = "DEAD_MAN_ACTIVE"
    else:
        state = "OK"

    return KeeperStatusResponse(
        state=state, last_heartbeat=last_heartbeat, consecutive_failures=failures
    )


def require_admin_token(x_admin_token: str = Header(...)):
    expected = os.getenv("KEEPER_ADMIN_TOKEN")
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


def _client_ip(request: Request) -> str | None:
    """
    Best-effort client IP for the key-access allowlist. Trusts the first entry of
    X-Forwarded-For only when KEY_ACCESS_TRUST_FORWARDED_FOR is set (i.e. the API
    is deployed behind a known reverse proxy / load balancer).
    """
    trust_xff = os.getenv("KEY_ACCESS_TRUST_FORWARDED_FOR", "").lower() in _TRUTHY
    if trust_xff:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def require_allowlisted_ip(request: Request):
    """
    IP allowlist for the key-management endpoints. KEY_ACCESS_IP_ALLOWLIST is a
    comma-separated list of IPs / CIDRs; an empty value disables the check
    (local dev). Rejects with 403 otherwise.
    """
    raw = os.getenv("KEY_ACCESS_IP_ALLOWLIST", "").strip()
    if not raw:
        return

    forbidden = HTTPException(
        status_code=403, detail="IP not allowlisted for key access"
    )
    client_ip = _client_ip(request)
    try:
        addr = ipaddress.ip_address(client_ip)
    except (ValueError, TypeError):
        raise forbidden from None

    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return
        except ValueError:
            continue
    raise forbidden


@router.post("/restart_circuit", dependencies=[Depends(require_admin_token)])
def restart_circuit():
    """
    Admin endpoint to reset the circuit breaker and dead-man switch after inspection.
    """
    reset_keeper_circuit()
    return {"status": "ok", "message": "Circuit reset successfully"}


@router.patch("/override", dependencies=[Depends(require_admin_token)])
def set_override(request: KeeperOverrideRequest):
    """Enables or disables the logged administrative policy override."""
    set_manual_override(request.manual_override)
    return {"manual_override": request.manual_override}


@router.get("/stats")
def get_stats():
    """Returns recent keeper rebalance activity and policy decisions."""
    return get_keeper_stats()


@router.post(
    "/emergency-revocate",
    dependencies=[Depends(require_admin_token), Depends(require_allowlisted_ip)],
)
def emergency_revocate(request: EmergencyRevokeRequest):
    """
    Emergency revocation of the active keeper signing key.

    Once revoked, the keeper bot refuses to sign any further transactions
    (services.key_manager.assert_signing_allowed) until a new key is rotated in.
    """
    row = revoke_active_signing_key(request.reason, actor="emergency-endpoint")
    if not row:
        raise HTTPException(status_code=404, detail="No active signing key to revoke")
    return {"status": "revoked", "key": row}


@router.post(
    "/rotate-key",
    dependencies=[Depends(require_admin_token), Depends(require_allowlisted_ip)],
)
def rotate_key():
    """
    Rotates the keeper signing key to the current quarter's KMS alias.
    Idempotent — a no-op when the current quarter's alias is already active.
    """
    return rotate_signing_key(actor="rotate-endpoint")


@router.get("/signing-audit", dependencies=[Depends(require_admin_token)])
def signing_audit(limit: int = 100):
    """Returns the immutable transaction-signing audit log, newest first."""
    return get_signing_audit_log(limit=limit)
