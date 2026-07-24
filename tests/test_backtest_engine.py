import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.backtest_engine import run_backtest


def _series(rates, start=datetime(2026, 1, 1), step=timedelta(hours=1)):
    return [(start + i * step, rate) for i, rate in enumerate(rates)]


def test_run_backtest_raises_on_insufficient_data():
    rows = _series([1500.0, 1501.0, 1502.0])
    with pytest.raises(ValueError, match="Insufficient data"):
        run_backtest(rows, volatility_window=10, threshold=80, initial_capital=10000, stable_apy=0)


def test_run_backtest_raises_on_non_positive_rate():
    rows = _series([1500.0, 0.0, 1502.0, 1503.0, 1504.0])
    with pytest.raises(ValueError, match="Non-positive rate"):
        run_backtest(rows, volatility_window=2, threshold=80, initial_capital=10000, stable_apy=0)


def test_run_backtest_flat_series_has_zero_return_and_no_switches():
    rows = _series([1500.0] * 20)
    result = run_backtest(rows, volatility_window=5, threshold=80, initial_capital=10000, stable_apy=0)

    assert result["data_points_used"] == 20
    assert result["strategy_metrics"]["total_return_pct"] == pytest.approx(0.0, abs=1e-9)
    assert result["baseline_metrics"]["total_return_pct"] == pytest.approx(0.0, abs=1e-9)
    assert result["comparison"]["return_improvement_pct"] == pytest.approx(0.0, abs=1e-9)


def test_run_backtest_shields_against_a_devaluation_spike():
    # Calm period, then a sharp devaluation (rate jump) partway through.
    calm = [1500.0 + (i % 3) * 0.01 for i in range(15)]
    spike = [1500.0 * (1.05 ** i) for i in range(1, 10)]
    rows = _series(calm + spike)

    result = run_backtest(rows, volatility_window=5, threshold=50, initial_capital=10000, stable_apy=0)

    # The strategy should outperform (or at least match) a naive buy-and-hold
    # through the devaluation, since it can shift to stable once volatility
    # crosses the threshold.
    assert result["strategy_metrics"]["total_return_pct"] >= result["baseline_metrics"]["total_return_pct"]
    assert result["comparison"]["return_improvement_pct"] >= 0
    assert result["strategy_metrics"]["num_regime_switches"] >= 1


def test_run_backtest_stable_apy_accrues_when_shielded():
    calm = [1500.0 + (i % 3) * 0.01 for i in range(15)]
    spike = [1500.0 * (1.05 ** i) for i in range(1, 10)]
    rows = _series(calm + spike, step=timedelta(days=1))

    result = run_backtest(rows, volatility_window=5, threshold=50, initial_capital=10000, stable_apy=5)

    assert result["strategy_metrics"]["time_in_stable_pct"] > 0
    assert result["strategy_metrics"]["final_value"] > 0


def test_run_backtest_with_lstm_model():
    calm = [1500.0 + (i % 3) * 0.01 for i in range(15)]
    spike = [1500.0 * (1.05 ** i) for i in range(1, 10)]
    rows = _series(calm + spike, step=timedelta(days=1))
    
    result = run_backtest(rows, volatility_window=5, threshold=50, initial_capital=10000, stable_apy=5, model_type="lstm")
    
    assert result["data_points_used"] == len(rows)
    assert result["strategy_metrics"]["final_value"] > 0
    assert "return_improvement_pct" in result["comparison"]


def test_run_backtest_with_gru_model():
    calm = [1500.0 + (i % 3) * 0.01 for i in range(15)]
    spike = [1500.0 * (1.05 ** i) for i in range(1, 10)]
    rows = _series(calm + spike, step=timedelta(days=1))
    
    result = run_backtest(rows, volatility_window=5, threshold=50, initial_capital=10000, stable_apy=5, model_type="gru")
    
    assert result["data_points_used"] == len(rows)
    assert result["strategy_metrics"]["final_value"] > 0
    assert "return_improvement_pct" in result["comparison"]


def test_run_backtest_raises_on_invalid_model_type():
    rows = _series([1500.0] * 20)
    with pytest.raises(ValueError, match="Invalid model_type"):
        run_backtest(rows, volatility_window=5, threshold=50, initial_capital=10000, stable_apy=0, model_type="invalid")
