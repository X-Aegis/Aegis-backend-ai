# X-Aegis Backend & AI 🤖⚙️

```text
╔════════════════════════════════════════════════════════════════════╗
║                                                                    ║
║           X - A E G I S   B A C K E N D   &   A I                  ║
║                                                                    ║
║               AI Forecasting & Automation Engine                   ║
║           Powering Volatility Protection on Stellar                ║
║                                                                    ║
╚════════════════════════════════════════════════════════════════════╝
```

> **The Intelligence Layer of the Aegis Volatility Shield.**

---

## 📈 Overview

This repository houses the off-chain intelligence and automation modules for the X-Aegis protocol. It is responsible for:
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

## 📚 Documentation
*   📘 **[Backend Roadmap](./docs/BACKEND_ROADMAP.md)**
*   🤖 **[AI Model Guide](./docs/AI_MODEL_GUIDE.md)**

---

## 🤝 Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

---

*Project maintained by @bbkenny.*
