"""services/keeper_metrics.py

Prometheus exporter for the keeper bot process (BK-15a / issue #36).

The keeper runs as its own long-lived process and is scraped on
``:8001/metrics`` by the ``aegis-keeper`` Prometheus job. This module starts a
Prometheus HTTP server and keeps a small set of gauges fresh from the keeper's
own state, refreshed after each poll-and-rebalance cycle.

The gauge definitions are shared with the API exporter (``api.metrics``) so the
same metric names are used in both processes.
"""

import logging
import os

from prometheus_client import start_http_server

from api.metrics import (
    KEEPER_REBALANCE_FAILURES,
    KEEPER_REBALANCES_LAST_24H,
    KEEPER_UPTIME_PERCENT,
)

logger = logging.getLogger("keeper_metrics")


def start_keeper_metrics_server(port: int | None = None) -> None:
    """Start the Prometheus scrape endpoint for the keeper process (default :8001)."""
    port = port or int(os.getenv("KEEPER_METRICS_PORT", "8001"))
    start_http_server(port)
    logger.info("Keeper metrics server listening on :%s/metrics", port)


def refresh_keeper_metrics(
    uptime_percent: float,
    rebalances_last_24h: int,
    rebalance_failures: int,
) -> None:
    """Refresh the exported gauges with the keeper's latest state."""
    KEEPER_UPTIME_PERCENT.set(uptime_percent)
    KEEPER_REBALANCES_LAST_24H.set(rebalances_last_24h)
    KEEPER_REBALANCE_FAILURES.set(rebalance_failures)
