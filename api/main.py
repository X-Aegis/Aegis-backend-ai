from fastapi import FastAPI

from api.backtest import router as backtest_router
from api.chat import router as chat_router
from api.keeper import router as keeper_router
from api.monitoring import router as monitoring_router
from api.risk import router as risk_router

app = FastAPI(title="X-Aegis Backend & AI", version="0.1.0")

app.include_router(backtest_router)
app.include_router(risk_router)
app.include_router(monitoring_router)
app.include_router(chat_router)
app.include_router(keeper_router)

@app.get("/")
def root():
    return {"status": "ok", "service": "x-aegis-backend"}
