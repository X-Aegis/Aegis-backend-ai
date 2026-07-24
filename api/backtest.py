import os
import sys
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

# Add project root to sys.path for local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.database import get_fx_rate_series, save_backtest_result, list_backtest_results
from services.backtest_engine import run_backtest

router = APIRouter(prefix="/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    pair: str = Field(..., description="FX pair as stored in fx_rates, e.g. 'USD/NGN'")
    start_date: date
    end_date: date
    strategy_name: str = Field("default", description="Label used to compare variants later")
    volatility_window: int = Field(24, ge=2, le=500, description="Rolling window size (periods) for the volatility signal")
    threshold: float = Field(80.0, ge=0, le=100, description="Volatility score above which the strategy shifts to stable")
    initial_capital: float = Field(10000.0, gt=0)
    stable_apy: float = Field(0.0, ge=0, le=100, description="Annualized yield earned while allocated to stable")
    model_type: str = Field("realized", description="Model type used: 'realized', 'lstm', or 'gru'")


class BacktestMetrics(BaseModel):
    final_value: float
    total_return_pct: float
    max_drawdown_pct: float
    num_regime_switches: int
    time_in_stable_pct: float


class BacktestComparison(BaseModel):
    return_improvement_pct: float
    drawdown_reduction_pct: float


class BacktestResult(BaseModel):
    id: int
    created_at: datetime
    strategy_name: str
    pair: str
    start_date: date
    end_date: date
    data_points_used: int
    params: dict
    strategy_metrics: BacktestMetrics
    baseline_metrics: BacktestMetrics
    comparison: BacktestComparison


@router.post("", response_model=BacktestResult, status_code=201)
def create_backtest(request: BacktestRequest):
    """Runs a backtest for the given strategy params and date range, and stores the report."""
    if request.start_date >= request.end_date:
        raise HTTPException(status_code=400, detail="start_date must be before end_date")

    rate_rows = get_fx_rate_series(request.pair, request.start_date, request.end_date)
    if not rate_rows:
        raise HTTPException(
            status_code=404,
            detail=f"No fx_rates found for pair '{request.pair}' between {request.start_date} and {request.end_date}",
        )

    try:
        outcome = run_backtest(
            rate_rows=rate_rows,
            volatility_window=request.volatility_window,
            threshold=request.threshold,
            initial_capital=request.initial_capital,
            stable_apy=request.stable_apy,
            model_type=request.model_type,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    params = {
        "volatility_window": request.volatility_window,
        "threshold": request.threshold,
        "initial_capital": request.initial_capital,
        "stable_apy": request.stable_apy,
        "model_type": request.model_type,
    }

    record = save_backtest_result(
        strategy_name=request.strategy_name,
        pair=request.pair,
        start_date=request.start_date,
        end_date=request.end_date,
        data_points_used=outcome["data_points_used"],
        params=params,
        strategy_metrics=outcome["strategy_metrics"],
        baseline_metrics=outcome["baseline_metrics"],
        comparison=outcome["comparison"],
    )

    return BacktestResult(
        id=record["id"],
        created_at=record["created_at"],
        strategy_name=request.strategy_name,
        pair=request.pair,
        start_date=request.start_date,
        end_date=request.end_date,
        data_points_used=outcome["data_points_used"],
        params=params,
        strategy_metrics=outcome["strategy_metrics"],
        baseline_metrics=outcome["baseline_metrics"],
        comparison=outcome["comparison"],
    )


@router.get("/results", response_model=list[BacktestResult])
def get_backtest_results(
    pair: Optional[str] = None,
    strategy_name: Optional[str] = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Retrieves past backtest reports, optionally filtered by pair and/or strategy_name, for comparing variants."""
    rows = list_backtest_results(pair=pair, strategy_name=strategy_name, limit=limit, offset=offset)
    return [BacktestResult(**row) for row in rows]
