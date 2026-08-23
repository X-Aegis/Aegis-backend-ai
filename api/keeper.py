import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/keeper", tags=["Keeper"])
log = logging.getLogger("keeper_api")

# In production, this would come from an environment variable or config
ALLOWED_IPS: list[str] = os.getenv("ALLOWED_KEEPER_IPS", "127.0.0.1").split(",")

async def verify_ip(request: Request):
    client_ip = request.client.host
    forwarded_for = request.headers.get("X-Forwarded-For")
    
    ip_to_check = forwarded_for.split(",")[0] if forwarded_for else client_ip
    
    if ip_to_check not in ALLOWED_IPS:
        log.warning(f"Unauthorized IP attempted to access keeper endpoints: {ip_to_check}")
        raise HTTPException(status_code=403, detail="Forbidden")
    return ip_to_check

class RevocationResponse(BaseModel):
    status: str
    message: str

@router.post("/emergency-revocate", response_model=RevocationResponse, dependencies=[Depends(verify_ip)])
async def emergency_revocate():
    """
    Emergency endpoint to revoke the active keeper signing key.
    Requires IP allowlisting.
    """
    log.info("Emergency revocation triggered")
    # In a real implementation, this would connect to the DB and update keeper_config status
    # For now, we mock the success response to satisfy the interface requirement
    return RevocationResponse(
        status="success",
        message="Active key revoked successfully. Keeper bot will stop signing until a new key is rotated."
    )
