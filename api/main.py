import time

from fastapi import FastAPI, Request

from api.backtest import router as backtest_router
from api.chat import router as chat_router
from api.fx import router as fx_router
from api.health import router as health_router
from api.keeper import router as keeper_router
from api.metrics import HTTP_REQUEST_DURATION
from api.metrics import router as metrics_router
from api.monitoring import router as monitoring_router
from api.risk import router as risk_router

app = FastAPI(title="X-Aegis Backend & AI", version="0.1.0")


@app.middleware("http")
async def observe_request_latency(request: Request, call_next):
    """Record request latency into the Prometheus histogram (BK-15a)."""
    start = time.perf_counter()
    response = await call_next(request)
    HTTP_REQUEST_DURATION.observe(time.perf_counter() - start)
    return response


app.include_router(backtest_router)
app.include_router(fx_router)
app.include_router(risk_router)
app.include_router(monitoring_router)
app.include_router(chat_router)
app.include_router(keeper_router)
app.include_router(health_router)
app.include_router(metrics_router)


@app.get("/")
def root():
    return {"status": "ok", "service": "x-aegis-backend"}
