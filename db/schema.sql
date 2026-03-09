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
