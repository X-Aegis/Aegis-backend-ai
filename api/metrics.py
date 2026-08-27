"""api/metrics.py

Prometheus metrics exporter (BK-15a / issue #36).

Serves ``GET /metrics`` in the Prometheus text format and gathers:

* prediction accuracy — rolling MAE / RMSE from ``drift_events``
* API request latency — ``http_request_duration_seconds`` histogram (p50/p95)
* keeper health — uptime %, rebalance frequency and failure count

All database reads are best-effort: if the database is unreachable the
exporter still serves ``/metrics`` with the previously exported values.
"""

import logging
import os
import sys
from datetime import datetime, timezone

logger = logging.getLogger("api.metrics")

from fastapi import APIRouter, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    Gauge,
    Histogram,
    generate_latest,
)

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import get_drift_summary, get_keeper_stats, get_keeper_status

router = APIRouter(tags=["metrics"])

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

PREDICTION_MAE = Gauge("prediction_mae", "Rolling mean absolute error")
PREDICTION_RMSE = Gauge("prediction_rmse", "Rolling root mean squared error")

KEEPER_UPTIME_PERCENT = Gauge(
    "keeper_uptime_percent",
    "Keeper uptime percentage (100 - hours since heartbeat / 24)",
)
KEEPER_REBALANCES_LAST_24H = Gauge(
    "keeper_rebalances_last_24h", "Number of keeper rebalances in the last 24 hours"
)
KEEPER_REBALANCE_FAILURES = Gauge(
    "keeper_rebalance_failures", "Number of failed keeper rebalance attempts"
)


def _refresh_prediction_accuracy() -> None:
    try:
        summary = get_drift_summary(pair="USD/NGN", horizon=1, limit=1)
        if not summary:
            return
        row = summary[0]
        if row.get("rolling_mae") is not None:
            PREDICTION_MAE.set(float(row["rolling_mae"]))
        if row.get("rolling_rmse") is not None:
            PREDICTION_RMSE.set(float(row["rolling_rmse"]))
    except Exception as exc:  # noqa: BLE001 - best-effort metrics export
        logger.debug("metrics refresh skipped: %s", exc)


def _keeper_uptime_percent(status: dict | None) -> float:
    if not status:
        return 0.0
    last_heartbeat = status.get("last_heartbeat")
    if not last_heartbeat:
        return 0.0
    try:
        hours_since = (
            datetime.now(timezone.utc) - last_heartbeat
        ).total_seconds() / 3600
    except TypeError:
        return 0.0
    return max(0.0, 100.0 - (hours_since / 24.0) * 100.0)


def _refresh_keeper_metrics() -> None:
    try:
        status = get_keeper_status()
        KEEPER_UPTIME_PERCENT.set(_keeper_uptime_percent(status))

        stats = get_keeper_stats() or {}
        KEEPER_REBALANCES_LAST_24H.set(float(stats.get("count_last_24h") or 0))
        KEEPER_REBALANCE_FAILURES.set(float(status.get("consecutive_failures") or 0))
    except Exception as exc:  # noqa: BLE001 - best-effort metrics export
        logger.debug("metrics refresh skipped: %s", exc)


def _refresh_metrics() -> None:
    _refresh_prediction_accuracy()
    _refresh_keeper_metrics()


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Prometheus scrape endpoint (Prometheus text exposition format)."""
    _refresh_metrics()
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
