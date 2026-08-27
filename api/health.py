"""api/health.py

Service health endpoint (BK-15a / issue #36).

``GET /health`` reports service status plus per-dependency checks:

* database — PostgreSQL/TimescaleDB reachability
* redis — cache reachability (skipped when ``REDIS_URL`` is unset)
* stellar_rpc — Soroban RPC ``getHealth`` (skipped when ``SOROBAN_RPC_URL`` is unset)
"""

import os
import sys

import httpx
from fastapi import APIRouter

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

router = APIRouter(tags=["health"])


def _check_database() -> dict:
    if not os.getenv("DATABASE_URL"):
        return {"status": "not_configured"}
    try:
        from lib.database import get_connection

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        finally:
            conn.close()
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 - health check must report, not raise
        return {"status": "error", "detail": str(exc)}


def _check_redis() -> dict:
    url = os.getenv("REDIS_URL")
    if not url:
        return {"status": "not_configured"}
    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(url, socket_timeout=2)
        try:
            client.ping()
        finally:
            client.close()
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 - health check must report, not raise
        return {"status": "error", "detail": str(exc)}


def _check_stellar_rpc() -> dict:
    url = os.getenv("SOROBAN_RPC_URL")
    if not url:
        return {"status": "not_configured"}
    try:
        resp = httpx.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
            timeout=3.0,
        )
        if resp.status_code != 200:
            return {"status": "error", "detail": f"http {resp.status_code}"}
        body = resp.json()
        if body.get("result"):
            return {"status": "ok"}
        return {"status": "error", "detail": str(body.get("error"))}
    except Exception as exc:  # noqa: BLE001 - health check must report, not raise
        return {"status": "error", "detail": str(exc)}


@router.get("/health")
def health() -> dict:
    """Service status plus dependency checks (database, redis, Stellar RPC)."""
    checks = {
        "database": _check_database(),
        "redis": _check_redis(),
        "stellar_rpc": _check_stellar_rpc(),
    }
    overall = (
        "ok" if all(c.get("status") == "ok" for c in checks.values()) else "degraded"
    )
    return {
        "status": overall,
        "service": "x-aegis-backend",
        "checks": checks,
    }
