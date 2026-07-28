import os
import sys

# Add project root to sys.path for local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI

from api.backtest import router as backtest_router
from api.monitoring import router as monitoring_router
from api.risk import router as risk_router

app = FastAPI(title="X-Aegis Backend & AI", version="0.1.0")

app.include_router(backtest_router)
app.include_router(risk_router)
app.include_router(monitoring_router)


@app.get("/")
def root():
    return {"status": "ok", "service": "x-aegis-backend"}
