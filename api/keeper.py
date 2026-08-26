import os
import sys
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import (
    get_keeper_stats,
    get_keeper_status,
    reset_keeper_circuit,
    set_manual_override,
)

router = APIRouter(prefix="/keeper", tags=["keeper"])


class KeeperStatusResponse(BaseModel):
    state: str
    last_heartbeat: datetime
    consecutive_failures: int


class KeeperOverrideRequest(BaseModel):
    manual_override: bool


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
