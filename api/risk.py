import os
import sys
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

# Add project root to sys.path for local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.database import get_current_prediction, get_prediction_history

router = APIRouter(prefix="/risk", tags=["risk"])


class PredictionPoint(BaseModel):
    timestamp: datetime
    horizon: int = Field(..., description="Prediction horizon in hours ahead")
    volatility_score: float = Field(
        ...,
        ge=0,
        le=100,
        description="Volatility risk score in the range [0, 100]. "
        "Higher values indicate greater predicted volatility.",
    )


class CurrentRiskResponse(BaseModel):
    timestamp: datetime
    horizon: int
    volatility_score: float = Field(..., ge=0, le=100)
    risk_level: str = Field(
        ...,
        description="Categorical label derived from volatility_score: "
        "LOW (< 40), MEDIUM (40–79), HIGH (>= 80).",
    )


def _risk_level(score: float) -> str:
    if score >= 80:
        return "HIGH"
    if score >= 40:
        return "MEDIUM"
    return "LOW"


@router.get("/current", response_model=CurrentRiskResponse)
def get_current_risk(
    horizon: int = Query(1, ge=1, description="Prediction horizon in hours ahead"),
):
    """
    Returns the most recent volatility risk prediction for the given horizon.

    The ``risk_level`` field is a convenience label derived from
    ``volatility_score``:

    - **LOW** — score < 40
    - **MEDIUM** — score 40–79
    - **HIGH** — score ≥ 80
    """
    row = get_current_prediction(horizon=horizon)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No predictions found for horizon={horizon}. "
            "The model may not have run yet.",
        )

    return CurrentRiskResponse(
        timestamp=row["timestamp"],
        horizon=row["horizon"],
        volatility_score=float(row["volatility_score"]),
        risk_level=_risk_level(float(row["volatility_score"])),
    )


@router.get("/history", response_model=list[PredictionPoint])
def get_risk_history(
    horizon: int = Query(1, ge=1, description="Prediction horizon in hours ahead"),
    limit: int = Query(
        100, ge=1, le=1000, description="Maximum number of data points to return"
    ),
    offset: int = Query(0, ge=0, description="Number of rows to skip (for pagination)"),
):
    """
    Returns historical volatility risk predictions for the given horizon,
    ordered most-recent first. Intended for rendering time-series charts on
    the frontend.

    Use ``limit`` and ``offset`` to paginate through large histories.
    """
    rows = get_prediction_history(horizon=horizon, limit=limit, offset=offset)
    return [
        PredictionPoint(
            timestamp=row["timestamp"],
            horizon=row["horizon"],
            volatility_score=float(row["volatility_score"]),
        )
        for row in rows
    ]
