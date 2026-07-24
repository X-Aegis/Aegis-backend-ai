-- db/schema.sql
-- Initial schema setup for X-Aegis FX forecasting engine

-- Setup timescaledb extension if not exists
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Table to store historical FX rates
CREATE TABLE IF NOT EXISTS fx_rates (
    "timestamp" TIMESTAMPTZ NOT NULL,
    pair VARCHAR(10) NOT NULL,
    rate NUMERIC NOT NULL,
    source VARCHAR(50) NOT NULL,
    PRIMARY KEY ("timestamp", pair)
);

-- Convert fx_rates to a hypertable for timeseries optimization
SELECT create_hypertable('fx_rates', 'timestamp', if_not_exists => TRUE);

-- Table to store model predictions
CREATE TABLE IF NOT EXISTS predictions (
    "timestamp" TIMESTAMPTZ NOT NULL,
    horizon INTEGER NOT NULL, -- Prediction horizon (e.g., hours ahead)
    volatility_score NUMERIC NOT NULL, -- Core volatility score (0-100)
    PRIMARY KEY ("timestamp", horizon)
);

-- Convert predictions to a hypertable
SELECT create_hypertable('predictions', 'timestamp', if_not_exists => TRUE);

-- Table to store periodic vault snapshots
CREATE TABLE IF NOT EXISTS vault_snapshots (
    "timestamp" TIMESTAMPTZ NOT NULL,
    tvl NUMERIC NOT NULL,
    share_price NUMERIC NOT NULL,
    PRIMARY KEY ("timestamp")
);

-- Convert vault_snapshots to a hypertable
SELECT create_hypertable('vault_snapshots', 'timestamp', if_not_exists => TRUE);

-- Table to store sentiment signals collected from social media and news sources
CREATE TABLE IF NOT EXISTS sentiment_data (
    "timestamp" TIMESTAMPTZ NOT NULL,
    source VARCHAR(50) NOT NULL, -- e.g. Twitter/X, Nairametrics
    keyword VARCHAR(100) NOT NULL, -- matched keyword, e.g. Naira, CBN, inflation
    content TEXT NOT NULL, -- original post/article text used for scoring
    sentiment_score NUMERIC NOT NULL -- compound sentiment score, range -1 (negative) to 1 (positive)
);

-- Convert sentiment_data to a hypertable for timeseries optimization
SELECT create_hypertable('sentiment_data', 'timestamp', if_not_exists => TRUE);

-- Index to support lookups by keyword over time
CREATE INDEX IF NOT EXISTS idx_sentiment_data_keyword ON sentiment_data (keyword, "timestamp" DESC);

-- Table to store backtest reports for strategy comparison.
-- Not a hypertable: rows are created one-per-API-call (low frequency results
-- log), not a continuously ingested time series like the tables above.
CREATE TABLE IF NOT EXISTS backtest_results (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    strategy_name VARCHAR(100) NOT NULL,
    pair VARCHAR(10) NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    data_points_used INTEGER NOT NULL,
    params JSONB NOT NULL, -- input strategy params (window, threshold, capital, apy)
    strategy_metrics JSONB NOT NULL, -- performance of the volatility-shifting strategy
    baseline_metrics JSONB NOT NULL, -- performance of a buy-and-hold baseline
    comparison JSONB NOT NULL -- strategy vs baseline comparison (the report)
);

-- Indexes to support "past reports" lookups by variant
CREATE INDEX IF NOT EXISTS idx_backtest_results_pair_created ON backtest_results (pair, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_results_strategy_created ON backtest_results (strategy_name, created_at DESC);

-- Table to store Keeper Bot rebalance events
-- Records every poll cycle where a rebalance was submitted, skipped, or failed.
CREATE TABLE IF NOT EXISTS rebalance_events (
    id BIGSERIAL PRIMARY KEY,
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now(),
    volatility_score NUMERIC NOT NULL,          -- score observed at the time of the cycle
    threshold NUMERIC NOT NULL,                 -- configured threshold used for the decision
    previous_allocation VARCHAR(10) NOT NULL,   -- allocation before this cycle: "risky" | "stable"
    target_allocation VARCHAR(10) NOT NULL,     -- desired allocation after this cycle
    status VARCHAR(20) NOT NULL,                -- "submitted" | "skipped" | "failed"
    tx_hash TEXT,                               -- Soroban transaction hash (NULL if not submitted)
    error_message TEXT                          -- error detail when status = "failed"
);

-- Index for querying rebalance history chronologically
CREATE INDEX IF NOT EXISTS idx_rebalance_events_timestamp ON rebalance_events ("timestamp" DESC);
