"""services/eda_volatility.py

Exploratory Data Analysis (EDA) for analyzing historical FX volatility clusters.
Identifies volatility persistence and clustering (large returns followed by large returns).
"""

import os
import sys
import numpy as np

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.database import get_fx_rate_series

def calculate_autocorrelation(series, max_lag=10):
    """Calculates autocorrelation for lags 1 to max_lag."""
    n = len(series)
    if n <= max_lag:
        return {}
    
    mean = np.mean(series)
    var = np.var(series)
    if var == 0:
        return {lag: 0.0 for lag in range(1, max_lag + 1)}
        
    autocorr = {}
    for lag in range(1, max_lag + 1):
        cov = np.sum((series[:-lag] - mean) * (series[lag:] - mean)) / n
        autocorr[lag] = float(cov / var)
    return autocorr

def analyze_volatility_clusters(rates, pair_name="USD/NGN", window=5):
    """
    Performs EDA on historical rates to identify volatility clusters.
    """
    rates = np.array(rates, dtype=float)
    if len(rates) < window + 2:
        return {
            "error": "Insufficient data to perform EDA analysis.",
            "data_points": len(rates)
        }
        
    returns = np.diff(rates) / rates[:-1]
    abs_returns = np.abs(returns)
    sq_returns = returns ** 2
    
    # Calculate rolling volatility
    rolling_vols = []
    for i in range(len(returns)):
        if i < window - 1:
            rolling_vols.append(np.nan)
        else:
            rolling_vols.append(np.std(returns[i - window + 1 : i + 1]))
    rolling_vols = np.array(rolling_vols)
    
    # Identify high volatility clusters (above 80th percentile of rolling volatility)
    valid_vols = rolling_vols[~np.isnan(rolling_vols)]
    if len(valid_vols) > 0:
        threshold = np.percentile(valid_vols, 80)
        high_vol_indices = np.where(rolling_vols >= threshold)[0]
        cluster_percentage = len(high_vol_indices) / len(valid_vols) * 100.0
    else:
        threshold = 0.0
        high_vol_indices = []
        cluster_percentage = 0.0
        
    # Autocorrelation of raw returns vs absolute returns
    ac_raw = calculate_autocorrelation(returns, max_lag=5)
    ac_abs = calculate_autocorrelation(abs_returns, max_lag=5)
    ac_sq = calculate_autocorrelation(sq_returns, max_lag=5)
    
    analysis = {
        "pair": pair_name,
        "total_periods": len(rates),
        "mean_return": float(np.mean(returns)),
        "std_return": float(np.std(returns)),
        "volatility_threshold_80th": float(threshold),
        "cluster_percentage_above_80th": float(cluster_percentage),
        "autocorr_raw_returns": ac_raw,
        "autocorr_abs_returns": ac_abs,
        "autocorr_sq_returns": ac_sq,
        "clustering_confirmed": any(ac_abs.get(lag, 0.0) > ac_raw.get(lag, 0.0) for lag in range(1, 4))
    }
    return analysis

def run_eda_cli():
    """Fetches series from database and runs the analysis, or uses simulated data if DB is empty."""
    from datetime import date, timedelta
    
    print("X-Aegis FX Volatility Clustering Analysis (EDA)")
    print("=" * 50)
    
    # Attempt to fetch USD/NGN rates from last 30 days
    end = date.today()
    start = end - timedelta(days=90)
    
    rate_rows = []
    try:
        rate_rows = get_fx_rate_series("USD/NGN", start, end)
    except Exception as e:
        print(f"Could not connect to database: {e}")
        
    if rate_rows:
        rates = [r[1] for r in rate_rows]
        print(f"Loaded {len(rates)} actual rates from database.")
        analysis = analyze_volatility_clusters(rates, pair_name="USD/NGN")
    else:
        print("No rates found in database. Using simulated rate sequence to demonstrate clustering...")
        # Simulating clustering (GARCH-like behavior: periods of low volatility and high volatility)
        np.random.seed(42)
        sim_rates = [1500.0]
        vol = 0.002
        for i in range(100):
            # Volatility is autoregressive (high vol follows high vol)
            if i in range(30, 50) or i in range(75, 90):
                vol = 0.8 * vol + 0.2 * 0.03 # high vol regime
            else:
                vol = 0.9 * vol + 0.1 * 0.001 # low vol regime
            ret = np.random.normal(0, vol)
            sim_rates.append(sim_rates[-1] * (1 + ret))
            
        analysis = analyze_volatility_clusters(sim_rates, pair_name="USD/NGN (Simulated)")
        
    for k, v in analysis.items():
        if isinstance(v, dict):
            print(f"{k}:")
            for lag, ac in v.items():
                print(f"  Lag {lag}: {ac:.4f}")
        else:
            print(f"{k}: {v}")

if __name__ == "__main__":
    run_eda_cli()
