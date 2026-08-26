"""api/risk.py

FastAPI router serving the volatility risk score.

``GET /risk/current`` is computed from **live** ``fx_rates`` history written by
``services/forex_ingester.py`` — realized volatility over a rolling window,
rescaled to the requested horizon (see ``services/live_risk.py``). Nothing here
reads seeded or stubbed data: if the live history is missing, stale or too
short, the endpoint fails loudly with a 404/503 rather than returning a number
a user cannot trust.

``GET /risk/history`` returns the stored model predictions used for charting and
drift monitoring.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

# Add project root to sys.path for local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.database import get_fx_rate_window, get_prediction_history
from lib.fx_utils import (
    MARKET_ANY,
    UnsupportedPairError,
    canonical_pair,
    ensure_utc,
    is_fresh,
    max_age_for_source,
    rate_age_seconds,
    source_filters,
)
from services.live_risk import (
    DEFAULT_WINDOW_HOURS,
    MIN_DATA_POINTS,
    InsufficientRateHistory,
    compute_live_risk,
)

router = APIRouter(prefix="/risk", tags=["risk"])

Market = Literal["any", "official", "parallel"]

MARKET_QUERY = Query(
    MARKET_ANY,
    description="Feed to score: 'parallel' (street rates), 'official' or 'any'. "
    "With 'any', a parallel-market series is preferred when one is available.",
)


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
    timestamp: datetime = Field(
        ..., description="Timestamp of the newest live rate the score is based on"
    )
    horizon: int
    volatility_score: float = Field(..., ge=0, le=100)
    risk_level: str = Field(
        ...,
        description="Categorical label derived from volatility_score: "
        "LOW (< 40), MEDIUM (40–79), HIGH (>= 80).",
    )
    pair: str = Field(..., description="Canonical pair the score was computed on")
    source: str = Field(..., description="Feed the rate series came from")
    market: str = Field(..., description="'official' or 'parallel'")
    latest_rate: float = Field(..., description="Newest live rate in the series")
    rate_age_seconds: float = Field(
        ..., description="Age of that rate, in seconds, at request time"
    )
    data_points: int = Field(..., description="Live observations used in the window")
    window_hours: int = Field(..., description="Lookback window used, in hours")
    realized_volatility: float = Field(
        ...,
        description="Standard deviation of log returns rescaled to the horizon "
        "(0.02 = a 2% expected move)",
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
    pair: str = Query(
        "USD/NGN", description="FX pair in either orientation, e.g. 'NGN/USD'"
    ),
    market: Market = MARKET_QUERY,
    window_hours: int = Query(
        DEFAULT_WINDOW_HOURS,
        ge=1,
        le=720,
        description="Lookback window of live rates used to compute volatility",
    ),
    allow_stale: bool = Query(
        False, description="Score the history even if the newest rate is stale"
    ),
):
    """
    Returns the current volatility risk for ``pair``, computed from live FX rate
    history.

    The score is realized volatility — the standard deviation of log returns
    over ``window_hours`` of ingested rates, rescaled to ``horizon`` hours — and
    ``risk_level`` is the convenience label derived from it:

    - **LOW** — score < 40
    - **MEDIUM** — score 40–79
    - **HIGH** — score ≥ 80

    Guards (a score is never invented):

    - **404** — no live rates stored for the pair yet.
    - **503** — the newest rate is stale (older than ``FX_MAX_RATE_AGE_MINUTES``)
      and ``allow_stale`` is false, or fewer than
      ``MIN_DATA_POINTS`` usable observations are in the window.

    Because official and parallel quotes can diverge by 20%+, the score is
    always computed within a single source; the response names it in ``source``
    and ``market``.
    """
    try:
        canonical, _ = canonical_pair(pair)
    except UnsupportedPairError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    sources, exclude_sources = source_filters(market)
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    rows = get_fx_rate_window(
        canonical,
        since,
        sources=sources,
        exclude_sources=exclude_sources,
        limit=5000,
    )

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No live FX rates stored for '{canonical}' in the last "
                f"{window_hours}h. The ingester may not have run yet."
            ),
        )

    try:
        risk = compute_live_risk(rows, horizon=horizon)
    except InsufficientRateHistory as e:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Insufficient live rate history for '{canonical}': {e} "
                f"Ingestion runs every 15 minutes; at least {MIN_DATA_POINTS} "
                f"observations must accumulate in the {window_hours}h window."
            ),
        ) from e

    timestamp = ensure_utc(risk["latest_timestamp"])
    age = rate_age_seconds(timestamp)
    max_age = max_age_for_source(risk["source"])
    if not is_fresh(timestamp, max_age_minutes=max_age) and not allow_stale:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Stale rate history for '{canonical}': newest quote from "
                f"{risk['source']} is {age / 60:.1f} minutes old "
                f"(max {max_age}). Risk score withheld — "
                "pass allow_stale=true to score it anyway."
            ),
        )

    return CurrentRiskResponse(
        timestamp=timestamp,
        horizon=horizon,
        volatility_score=risk["volatility_score"],
        risk_level=_risk_level(risk["volatility_score"]),
        pair=canonical,
        source=risk["source"],
        market=risk["market"],
        latest_rate=risk["latest_rate"],
        rate_age_seconds=age,
        data_points=risk["data_points"],
        window_hours=window_hours,
        realized_volatility=risk["realized_volatility"],
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
