import os
import sys
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.database import list_rebalance_events

router = APIRouter(prefix="/rebalance", tags=["rebalance"])


class RebalanceEvent(BaseModel):
    id: int
    timestamp: datetime
    volatility_score: float
    threshold: float
    previous_allocation: str
    target_allocation: str
    status: str = Field(..., description="submitted | skipped | failed")
    tx_hash: Optional[str] = None
    error_message: Optional[str] = None


@router.get("/events", response_model=list[RebalanceEvent])
def get_rebalance_events(
    status: Optional[str] = Query(None, description="Filter by status: submitted | skipped | failed"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Returns the Keeper Bot's rebalance event log, most recent first."""
    rows = list_rebalance_events(status=status, limit=limit, offset=offset)
    return [RebalanceEvent(**row) for row in rows]
