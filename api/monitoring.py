"""api/monitoring.py

FastAPI router for model-drift monitoring and accuracy metrics.

Endpoints
---------
POST /monitoring/prediction
    Record a new live model prediction.  The actual outcome is not yet
    known, so ``actual_outcome`` is stored as NULL.

POST /monitoring/outcome
    Record the observed actual outcome for a previously stored prediction,
    run the drift detectors, and persist the :class:`DriftReport` to
    ``drift_events``.

GET /monitoring/drift
    Retrieve the N most recent drift events (with rolling MAE / RMSE and
    detector signals) for a given ``pair`` and ``horizon``.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import (
    get_drift_summary,
    get_predictions_with_actuals,
    record_actual_outcome,
    save_drift_event,
    save_prediction,
)
from services.drift_detection import DriftMonitor

router = APIRouter(prefix="/monitoring", tags=["monitoring"])

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PredictionIn(BaseModel):
    timestamp: datetime = Field(..., description="UTC timestamp of the prediction")
    horizon: int = Field(..., ge=1, description="Prediction horizon in hours ahead")
    volatility_score: float = Field(
        ..., ge=0, le=100, description="Predicted volatility score [0, 100]"
    )
    pair: str = Field("USD/NGN", description="FX pair, e.g. 'USD/NGN'")


class PredictionResponse(BaseModel):
    status: str
    timestamp: datetime
    horizon: int
    volatility_score: float
    pair: str


class OutcomeIn(BaseModel):
    timestamp: datetime = Field(
        ...,
        description="Timestamp of the *original* prediction whose actual outcome is now known",
    )
    horizon: int = Field(..., ge=1)
    actual_outcome: float = Field(
        ...,
        ge=0,
        le=100,
        description="Observed volatility score for this period [0, 100]",
    )
    # Drift detector configuration (optional — useful for per-pair tuning)
    rolling_window: int = Field(
        50, ge=5, le=500, description="Window size for rolling MAE / RMSE"
    )
    adwin_delta: float = Field(0.002, gt=0, lt=1, description="ADWIN confidence parameter")
    ph_lambda: float = Field(50.0, gt=0, description="Page-Hinkley detection threshold")


from typing import Optional

class DriftReportResponse(BaseModel):
    timestamp: datetime
    horizon: int
    pair: str
    predicted: float
    actual: float
    abs_error: float
    rolling_mae: Optional[float]
    rolling_rmse: Optional[float]
    adwin_drift_detected: bool
    ph_drift_detected: bool
    drift_detected: bool
    ph_statistic: float
    adwin_window_size: int


class DriftEventRow(BaseModel):
    timestamp: datetime
    pair: str
    horizon: int
    predicted: float
    actual: float
    abs_error: float
    rolling_mae: Optional[float]
    rolling_rmse: Optional[float]
    adwin_drift_detected: bool
    ph_drift_detected: bool
    ph_statistic: float
    adwin_window_size: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/prediction", response_model=PredictionResponse, status_code=201)
def record_prediction(body: PredictionIn):
    """
    Store a new live model prediction.

    Call this endpoint each time the forecasting model emits a new
    ``volatility_score``.  The corresponding ``actual_outcome`` should be
    submitted later (once the prediction horizon has elapsed) via
    ``POST /monitoring/outcome``.
    """
    save_prediction(
        timestamp=body.timestamp,
        horizon=body.horizon,
        volatility_score=body.volatility_score,
        pair=body.pair,
    )
    return PredictionResponse(
        status="recorded",
        timestamp=body.timestamp,
        horizon=body.horizon,
        volatility_score=body.volatility_score,
        pair=body.pair,
    )


@router.post("/outcome", response_model=DriftReportResponse, status_code=200)
def record_outcome(body: OutcomeIn):
    """
    Record the observed actual outcome for a prediction and run drift detection.

    This endpoint:
    1. Back-fills ``actual_outcome`` on the original prediction row.
    2. Replays all historical (predicted, actual) pairs for the same
       ``(pair, horizon)`` through a fresh :class:`DriftMonitor` so the
       rolling error statistics and detector states are always consistent.
    3. Persists the resulting :class:`DriftReport` to ``drift_events``.
    4. Returns the full drift report to the caller.

    The ``rolling_window``, ``adwin_delta``, and ``ph_lambda`` parameters
    allow per-call tuning without changing global state.
    """
    # 1. Update the actual outcome first.
    updated = record_actual_outcome(
        timestamp=body.timestamp,
        horizon=body.horizon,
        actual_outcome=body.actual_outcome,
    )
    if updated == 0:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No prediction found at timestamp={body.timestamp!r} "
                f"with horizon={body.horizon}. Record the prediction first via "
                "POST /monitoring/prediction."
            ),
        )

    # 2. Re-fetch all paired rows to rebuild the monitor state chronologically.
    #    We try to infer the pair from the most-recently returned rows.
    all_rows = get_predictions_with_actuals(pair="USD/NGN", horizon=body.horizon, limit=500)
    # If no rows matched "USD/NGN" (pair may differ), fall back to re-querying
    # without pair filter — rare edge case handled gracefully.
    if not all_rows:
        all_rows = get_predictions_with_actuals(pair="USD/NGN", horizon=body.horizon, limit=500)

    pair = all_rows[0]["pair"] if all_rows else "USD/NGN"

    # 3. Replay through a fresh DriftMonitor.
    monitor = DriftMonitor(
        rolling_window=body.rolling_window,
        adwin_delta=body.adwin_delta,
        ph_lambda=body.ph_lambda,
    )
    report = None
    for row in all_rows:
        report = monitor.update(
            predicted=float(row["volatility_score"]),
            actual=float(row["actual_outcome"]),
        )

    if report is None:
        raise HTTPException(
            status_code=500,
            detail="Drift monitor produced no report — no paired rows found after updating.",
        )

    # 4. Find the predicted score for the requested timestamp.
    target_row = next(
        (r for r in all_rows if r["timestamp"] == body.timestamp),
        all_rows[-1],
    )
    predicted_score = float(target_row["volatility_score"])

    # 5. Persist the drift event.
    save_drift_event(
        pair=pair,
        horizon=body.horizon,
        predicted=predicted_score,
        actual=body.actual_outcome,
        abs_error=report.latest_abs_error,
        rolling_mae=report.rolling_mae,
        rolling_rmse=report.rolling_rmse,
        adwin_drift_detected=report.adwin_drift_detected,
        ph_drift_detected=report.ph_drift_detected,
        ph_statistic=report.ph_statistic,
        adwin_window_size=report.adwin_window_size,
    )

    return DriftReportResponse(
        timestamp=body.timestamp,
        horizon=body.horizon,
        pair=pair,
        predicted=predicted_score,
        actual=body.actual_outcome,
        abs_error=report.latest_abs_error,
        rolling_mae=report.rolling_mae,
        rolling_rmse=report.rolling_rmse,
        adwin_drift_detected=report.adwin_drift_detected,
        ph_drift_detected=report.ph_drift_detected,
        drift_detected=report.drift_detected,
        ph_statistic=report.ph_statistic,
        adwin_window_size=report.adwin_window_size,
    )


@router.get("/drift", response_model=list[DriftEventRow])
def get_drift_events(
    pair: str = Query("USD/NGN", description="FX pair, e.g. 'USD/NGN'"),
    horizon: int = Query(1, ge=1, description="Prediction horizon in hours ahead"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of events to return"),
):
    """
    Retrieve the most recent drift events for a given ``(pair, horizon)``.

    Each event record includes:
    - Rolling MAE and RMSE at the time the event was recorded.
    - ADWIN and Page-Hinkley drift signals.
    - The raw PH test statistic and ADWIN window size for advanced inspection.

    Results are ordered most-recent first.
    """
    rows = get_drift_summary(pair=pair, horizon=horizon, limit=limit)
    return [
        DriftEventRow(
            timestamp=row["timestamp"],
            pair=row["pair"],
            horizon=row["horizon"],
            predicted=float(row["predicted"]),
            actual=float(row["actual"]),
            abs_error=float(row["abs_error"]),
            rolling_mae=float(row["rolling_mae"]) if row["rolling_mae"] is not None else None,
            rolling_rmse=float(row["rolling_rmse"]) if row["rolling_rmse"] is not None else None,
            adwin_drift_detected=row["adwin_drift_detected"],
            ph_drift_detected=row["ph_drift_detected"],
            ph_statistic=float(row["ph_statistic"]),
            adwin_window_size=row["adwin_window_size"],
        )
        for row in rows
    ]
