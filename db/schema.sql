-- db/schema.sql
-- Initial schema setup for X-Aegis FX forecasting engine

-- Setup timescaledb extension if not exists
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Table to store historical FX rates.
-- source is part of the primary key: the official vendors and the
-- parallel-market feed quote the same pair at the same minute with different
-- rates, and both prints must be kept (see db/migrations/001_fx_rates_multi_source.sql).
CREATE TABLE IF NOT EXISTS fx_rates (
    "timestamp" TIMESTAMPTZ NOT NULL,
    pair VARCHAR(10) NOT NULL,   -- canonical orientation, e.g. 'USD/NGN'
    rate NUMERIC NOT NULL,       -- units of the quote currency per 1 USD
    source VARCHAR(50) NOT NULL, -- e.g. Fixer.io, OpenExchangeRates, ParallelMarket
    PRIMARY KEY ("timestamp", pair, source)
);

-- Convert fx_rates to a hypertable for timeseries optimization
SELECT create_hypertable('fx_rates', 'timestamp', if_not_exists => TRUE);

-- Latest-rate and rolling-window lookups (GET /fx/current, GET /risk/current)
CREATE INDEX IF NOT EXISTS idx_fx_rates_pair_timestamp
ON fx_rates (pair, "timestamp" DESC);

-- Per-source freshness lookups (GET /fx/sources, source fallback checks)
CREATE INDEX IF NOT EXISTS idx_fx_rates_pair_source_timestamp
ON fx_rates (pair, source, "timestamp" DESC);

-- Table to store model predictions and their observed actual outcomes.
-- actual_outcome is nullable — populated later via the outcome recording endpoint.
-- pair identifies the FX pair the prediction relates to (e.g. 'USD/NGN').
CREATE TABLE IF NOT EXISTS predictions (
    "timestamp" TIMESTAMPTZ NOT NULL,
    horizon INTEGER NOT NULL,          -- Prediction horizon (e.g., hours ahead)
    volatility_score NUMERIC NOT NULL, -- Core volatility score (0-100)
    pair VARCHAR(10) NOT NULL DEFAULT 'USD/NGN', -- FX pair the prediction covers
    actual_outcome NUMERIC,            -- Observed volatility score once the period closes
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

-- ---------------------------------------------------------------------------
-- Drift monitoring
-- ---------------------------------------------------------------------------

-- Table to store individual drift-detector events and rolling accuracy metrics.
-- One row is written for every (prediction, actual) pair that is recorded;
-- callers can query for the latest row to get the current health of the model.
CREATE TABLE IF NOT EXISTS drift_events (
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now(),
    pair VARCHAR(10) NOT NULL,
    horizon INTEGER NOT NULL,
    predicted NUMERIC NOT NULL,
    actual NUMERIC NOT NULL,
    abs_error NUMERIC NOT NULL,            -- |predicted - actual|
    rolling_mae NUMERIC,                   -- MAE over recent window (NULL until window fills)
    rolling_rmse NUMERIC,                  -- RMSE over recent window (NULL until window fills)
    adwin_drift_detected BOOLEAN NOT NULL, -- ADWIN change-point signal
    ph_drift_detected BOOLEAN NOT NULL,    -- Page-Hinkley upward-drift signal
    ph_statistic NUMERIC NOT NULL,         -- Raw PH statistic at this point in time
    adwin_window_size INTEGER NOT NULL     -- Current ADWIN adaptive window size
);

-- Convert drift_events to a hypertable for efficient time-range queries
SELECT create_hypertable('drift_events', 'timestamp', if_not_exists => TRUE);

-- Index to support pair + horizon lookups (the most common query pattern)
CREATE INDEX IF NOT EXISTS idx_drift_events_pair_horizon ON drift_events (pair, horizon, "timestamp" DESC);

-- Table to store keeper bot circuit breaker status
CREATE TABLE IF NOT EXISTS keeper_status (
    id INTEGER PRIMARY KEY DEFAULT 1,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (id = 1)
);

INSERT INTO keeper_status (id, consecutive_failures, last_heartbeat)
VALUES (1, 0, now())
ON CONFLICT DO NOTHING;

-- Keeper rebalance policy state and its immutable audit trail.
CREATE TABLE IF NOT EXISTS keeper_policy (
    id INTEGER PRIMARY KEY DEFAULT 1,
    manual_override BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK (id = 1)
);

INSERT INTO keeper_policy (id, manual_override)
VALUES (1, FALSE)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS keeper_decisions (
    id BIGSERIAL PRIMARY KEY,
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now(),
    model_score NUMERIC NOT NULL,
    proposed_allocations JSONB NOT NULL,
    threshold_checks JSONB NOT NULL,
    decision VARCHAR(20) NOT NULL CHECK (decision IN ('approved', 'rejected')),
    transaction_submitted BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_keeper_decisions_timestamp
ON keeper_decisions ("timestamp" DESC);
