# Backend & AI Roadmap

This document tracks the FX forecasting engine and automation infrastructure. Active work is tracked as GitHub issues — link to them here.

---

## Phase 1: Data Pipeline

### BK-1: Live FX Data Ingestion — real NGN/KES rates
- **Status:** OPEN — [GitHub Issue #30](https://github.com/X-Aegis/aegis-backend-ai/issues/30)
- **User impact:** The dashboard and volatility model use live, real market rates instead of stubbed data.
- **Tasks:** ingester service, `fx_rates` storage, source fallback, stale-rate guard, `GET /fx/current`.

### BK-2: Database Schema
- **Status:** DONE as part of the rate/prediction/snapshot storage used by the risk API.

---

## Phase 2: AI Core

### BK-3: Volatility Prediction Model
- **Status:** OPEN — [GitHub Issue #5](https://github.com/X-Aegis/aegis-backend-ai/issues/5)
- EDA, LSTM/GRU training, `VolatilityScore` (0-100), backtest ("shift to stable if Score > 80").

### BK-4: Model API
- **Status:** DONE — `GET /risk/current` and `GET /risk/history` live in `api/risk.py`; drift monitoring in `api/monitoring.py`; backtesting in `api/backtest.py`.

---

## Phase 3: Automation

### BK-5: Automated Keeper Bot (Rebalancer)
- **Status:** OPEN — [GitHub Issue #31](https://github.com/X-Aegis/aegis-backend-ai/issues/31)
- **User impact:** The vault automatically rebalances when volatility risk crosses a threshold.
- **Tasks:** poll risk API, detect threshold breach, build+sign+submit `rebalance` tx, idempotent, logged.
