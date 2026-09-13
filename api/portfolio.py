"""Portfolio insight, allocation, simulation, and attribution endpoints."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from enum import Enum

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import get_current_prediction, get_recent_sentiment
from services.keeper_bot import compute_target_allocation

router = APIRouter(tags=["portfolio"])


class InsightRequest(BaseModel):
    horizon: int = Field(1, ge=1)
    limit: int = Field(20, ge=1, le=100)


class Insight(BaseModel):
    id: str
    timestamp: datetime
    message: str
    type: str


class AllocationResponse(BaseModel):
    timestamp: datetime
    horizon: int
    volatilityScore: float
    stablePercent: float
    riskyPercent: float


class YieldBreakdownRequest(BaseModel):
    address: str | None = None
    period: str = "1M"


class SimulationRequest(BaseModel):
    fxShockPercent: float
    volatilityShockPercent: float
    portfolioValueUsd: float = Field(..., gt=0)


class Projection(BaseModel):
    projectedValueUsd: float
    changePercent: float


class SimulationResponse(BaseModel):
    withoutAegis: Projection
    withAegis: Projection
    strategyShiftPercent: float


class AttributionPeriod(str, Enum):
    one_week = "1W"
    one_month = "1M"
    three_months = "3M"
    all_time = "All"


class AttributionSource(BaseModel):
    name: str
    value: float
    percentage: float
    color: str


class AttributionResponse(BaseModel):
    total: float
    sources: list[AttributionSource]


def _db_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Portfolio data is unavailable") from exc


def _prediction_or_404(horizon: int):
    row = _db_call(get_current_prediction, horizon=horizon)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No prediction data found for horizon={horizon}")
    return row


@router.post("/insights", response_model=list[Insight])
def create_insights(body: InsightRequest):
    prediction = _prediction_or_404(body.horizon)
    score = float(prediction["volatility_score"])
    insight_type = "WARNING" if score >= 80 else "INFO"
    message = (
        f"High volatility forecast ({score:.1f}/100); defensive allocation is recommended."
        if score >= 80
        else f"Volatility forecast is {score:.1f}/100; the current allocation remains growth-oriented."
    )
    result = [Insight(id="prediction-risk", timestamp=prediction["timestamp"], message=message, type=insight_type)]

    sentiment_rows = _db_call(get_recent_sentiment, limit=body.limit)
    for index, row in enumerate(sentiment_rows):
        sentiment = float(row["sentiment_score"])
        if abs(sentiment) < 0.1:
            continue
        result.append(
            Insight(
                id=f"sentiment-{index}",
                timestamp=row["timestamp"],
                message=f"{row['keyword']} sentiment is {'positive' if sentiment > 0 else 'negative'} ({sentiment:.2f}).",
                type="INFO" if sentiment > 0 else "WARNING",
            )
        )
    return result[: body.limit]


@router.post("/yield-breakdown", response_model=AttributionResponse)
def yield_breakdown(body: YieldBreakdownRequest):
    raise HTTPException(
        status_code=404,
        detail="No yield attribution data is stored for the requested portfolio",
    )


@router.get("/allocation", response_model=AllocationResponse)
def get_allocation(horizon: int = Query(1, ge=1)):
    row = _prediction_or_404(horizon)
    stable = compute_target_allocation(float(row["volatility_score"]))
    return AllocationResponse(
        timestamp=row["timestamp"],
        horizon=row["horizon"],
        volatilityScore=float(row["volatility_score"]),
        stablePercent=stable,
        riskyPercent=100.0 - stable,
    )


@router.get("/strategy/{address}")
def get_strategy(address: str):
    raise HTTPException(status_code=404, detail=f"No strategy data found for address '{address}'")


@router.post("/simulate", response_model=SimulationResponse)
def simulate(body: SimulationRequest):
    prediction = _prediction_or_404(1)
    baseline_score = float(prediction["volatility_score"])
    shocked_score = max(0.0, min(100.0, baseline_score + body.volatilityShockPercent))
    shift = compute_target_allocation(shocked_score)
    raw_change = body.fxShockPercent
    protected_change = raw_change * (1.0 - shift / 100.0)
    without_value = body.portfolioValueUsd * (1.0 + raw_change / 100.0)
    with_value = body.portfolioValueUsd * (1.0 + protected_change / 100.0)
    return SimulationResponse(
        withoutAegis=Projection(projectedValueUsd=round(without_value, 2), changePercent=round(raw_change, 2)),
        withAegis=Projection(projectedValueUsd=round(with_value, 2), changePercent=round(protected_change, 2)),
        strategyShiftPercent=shift,
    )


@router.get("/attribution", response_model=AttributionResponse)
def attribution(period: AttributionPeriod = AttributionPeriod.one_month):
    raise HTTPException(status_code=404, detail=f"No attribution data is available for period={period.value}")
