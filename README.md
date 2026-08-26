# XHedge (Aegis) Backend & AI 🤖⚙️

<p align="center">
  <img src="logo.jpeg" alt="XHedge (Aegis) Logo" width="200" />
</p>

```text
╔════════════════════════════════════════════════════════════════════╗
║                                                                    ║
║         X H E D G E ( A E G I S )   B A C K E N D   A I            ║
║                                                                    ║
║               AI Forecasting & Automation Engine                   ║
║           Powering Volatility Protection on Stellar                ║
║                                                                    ║
╚════════════════════════════════════════════════════════════════════╝
```

> **The Intelligence Layer of the XHedge (Aegis) Volatility Shield.**

## 🚀 Deployment Status
- **Backend API:** Deployed on Render
- **URL:** [https://aegis-backend-ai.onrender.com](https://aegis-backend-ai.onrender.com)
- **Status:** Live & processing AI risk algorithms

---

## 📈 Overview

This repository houses the off-chain intelligence and automation modules for the XHedge (Aegis) protocol. It is responsible for:
*   **FX Forecasting**: Time-series modeling to predict volatility spikes in emerging market currencies.
*   **Risk Oracles**: Serving real-time risk scores to the frontend and smart contracts.
*   **Keeper Automation**: Monitoring protocol conditions and triggering rebalancing transactions on Soroban.

---

## 🛠 Tech Stack

*   **Language**: Python 3.10+
*   **Framework**: FastAPI
*   **AI/ML**: Scikit-Learn, Pandas, Prophet/LSTM
*   **Data**: TimescaleDB / PostgreSQL
*   **Automation**: Soroban Python SDK

---

## 🏗 Project Structure

```text
├── api/                # FastAPI endpoints & risk oracles
├── models/             # AI/ML model training & inference
├── services/           # Data ingesters & keeper bots
├── notebooks/          # Exploratory Data Analysis (EDA)
└── docs/               # Documentation & implementation guides
```

---

## 🚀 Getting Started

### 1. Prerequisites
*   Python 3.10+
*   Virtualenv / Conda

### 2. Setup
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 📡 Live FX Data (NGN / KES)

Rates are **live**, not seeded. `services/forex_ingester.py` runs on a schedule
and writes every quote it validates into `fx_rates`:

```bash
cp .env.example .env                       # configure sources (all optional)
python -m services.forex_ingester          # loop, one cycle every 15 minutes
python -m services.forex_ingester --once   # single cycle (cron / CI)
```

**Sources** — the primary vendor API is tried first and the ingester fails over
when it is down, rate-limited or frozen:

| Order | Source | Market | Credentials |
|---|---|---|---|
| 1 | Fixer.io (`FX_PRIMARY_SOURCE=fixer`) | official | `FIXER_API_KEY` |
| 2 | Open Exchange Rates | official | `OPEN_EXCHANGE_RATES_APP_ID` |
| 3 | ExchangeRate-API open endpoint | official | none — keyless last resort |
| — | Parallel-market feed | parallel | `PARALLEL_MARKET_API_URL` |

The parallel-market feed runs alongside the official chain, never instead of it:
NGN street rates can sit 20%+ away from the official print, and that is the rate
users actually transact at.

**Freshness** — every quote is validated before it is stored, and again before
it is served. A continuously updating feed is stale after
`FX_MAX_RATE_AGE_MINUTES` (45 — three missed cycles); a once-a-day fallback gets
its own budget. Stale data is withheld with a `503`, never quietly served.

**Endpoints**

```bash
curl 'localhost:8000/fx/current?pair=NGN/USD'          # latest live rate
curl 'localhost:8000/fx/history?pair=USD/NGN&hours=24' # what the score used
curl 'localhost:8000/fx/sources'                       # per-feed health
curl 'localhost:8000/risk/current?pair=USD/NGN'        # volatility from live rates
```

`GET /risk/current` computes realized volatility from the stored rate history —
standard deviation of log returns over the window, rescaled to the horizon — and
is computed **within a single source** so official/parallel spreads never
masquerade as volatility. With too little or too stale history it returns
`404`/`503` rather than a number nobody can trust.

**See it run** (no database, no API keys — fetches real rates over the network):

```bash
python scripts/demo_live_fx.py
```

---

## 📚 Documentation
*   📘 **[Backend Roadmap](./docs/BACKEND_ROADMAP.md)**
*   🤖 **[AI Model Guide](./docs/AI_MODEL_GUIDE.md)**
*   🚨 **[Alerting rules & runbooks](./alerting/README.md)**

---

## 🤝 Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

---

*Project maintained by @bbkenny.*
