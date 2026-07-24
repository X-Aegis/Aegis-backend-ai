"""tests/test_volatility_model.py

Unit tests for VolatilityGRUModel, VolatilityLSTMModel, and VolatilityModelWrapper.
"""

import os
import sys
import pytest
import numpy as np

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.volatility_model import (
    VolatilityGRUModel,
    VolatilityLSTMModel,
    VolatilityModelWrapper,
)

def test_gru_model_forward():
    model = VolatilityGRUModel(seq_len=5, hidden_dim=4)
    X = np.random.randn(5, 1)
    y, cache = model.forward(X)
    assert 0.0 <= y <= 100.0
    assert "xs" in cache
    assert "hs" in cache
    assert len(cache["xs"]) == 5

def test_lstm_model_forward():
    model = VolatilityLSTMModel(seq_len=5, hidden_dim=4)
    X = np.random.randn(5, 1)
    y, cache = model.forward(X)
    assert 0.0 <= y <= 100.0
    assert "xs" in cache
    assert "hs" in cache
    assert "cs" in cache
    assert len(cache["xs"]) == 5

def test_gru_train_step():
    model = VolatilityGRUModel(seq_len=5, hidden_dim=4, lr=0.1)
    X = np.random.randn(5, 1)
    initial_loss = model.train_step(X, 80.0)
    
    # Run multiple train steps to ensure parameters change and loss converges
    losses = []
    for _ in range(5):
        losses.append(model.train_step(X, 80.0))
    
    assert losses[-1] <= initial_loss or losses[-1] < 1.0

def test_lstm_train_step():
    model = VolatilityLSTMModel(seq_len=5, hidden_dim=4, lr=0.1)
    X = np.random.randn(5, 1)
    initial_loss = model.train_step(X, 20.0)
    
    losses = []
    for _ in range(5):
        losses.append(model.train_step(X, 20.0))
        
    assert losses[-1] <= initial_loss or losses[-1] < 1.0

def test_wrapper_fit_and_predict_gru():
    wrapper = VolatilityModelWrapper(model_type="gru", seq_len=5, hidden_dim=4)
    # 20 rates
    rates = [100.0 + i + (i % 2) * 2.0 for i in range(20)]
    losses = wrapper.fit(rates, epochs=5, target_window=3)
    
    assert len(losses) == 5
    score = wrapper.predict(rates)
    assert 0.0 <= score <= 100.0

def test_wrapper_fit_and_predict_lstm():
    wrapper = VolatilityModelWrapper(model_type="lstm", seq_len=5, hidden_dim=4)
    rates = [100.0 + i + (i % 2) * 2.0 for i in range(20)]
    losses = wrapper.fit(rates, epochs=5, target_window=3)
    
    assert len(losses) == 5
    score = wrapper.predict(rates)
    assert 0.0 <= score <= 100.0

def test_wrapper_insufficient_data():
    wrapper = VolatilityModelWrapper(model_type="gru", seq_len=10)
    # Not enough data (needs seq_len + target_window + 1 = 10 + 5 + 1 = 16 rates)
    rates = [100.0] * 5
    losses = wrapper.fit(rates, epochs=2, target_window=5)
    assert losses == []
    
    score = wrapper.predict(rates)
    assert score == 50.0


def test_calculate_autocorrelation():
    from services.eda_volatility import calculate_autocorrelation
    series = [1.0, 2.0, 3.0, 4.0, 5.0]
    res = calculate_autocorrelation(series, max_lag=2)
    assert 1 in res
    assert 2 in res
    
    # Check limit when series is shorter than max_lag
    res_empty = calculate_autocorrelation(series, max_lag=10)
    assert res_empty == {}


def test_analyze_volatility_clusters():
    from services.eda_volatility import analyze_volatility_clusters
    rates = [100.0, 101.0, 102.0, 103.0, 102.0, 101.0, 100.0, 105.0]
    res = analyze_volatility_clusters(rates, window=3)
    assert "pair" in res
    assert "clustering_confirmed" in res
    assert res["total_periods"] == 8
    
    # Check error message with insufficient data
    res_err = analyze_volatility_clusters([100.0], window=5)
    assert "error" in res_err

