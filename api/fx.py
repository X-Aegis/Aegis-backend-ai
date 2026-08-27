"""api/fx.py

FastAPI router serving the live FX rates written by ``services/forex_ingester.py``.

Endpoints
---------
GET /fx/current
    The latest live rate for a pair, in whichever orientation the caller asked
    for (``NGN/USD`` or ``USD/NGN``), with an explicit freshness verdict.

GET /fx/history
    Recent rate history for charting and for auditing what the risk score was
    computed from.

GET /fx/sources
    Per-feed ingestion health — which sources are alive, how fresh they are and
    how many points landed in the last 24 hours.

Every response states its ``source`` and ``age_seconds`` so consumers can see
exactly which market (official vs parallel) a number came from and how old it is.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import get_fx_rate_window, get_fx_source_status, get_latest_fx_rate
from lib.fx_utils import (
    MARKET_ANY,
    MAX_RATE_AGE_MINUTES,
    UnsupportedPairError,
    canonical_pair,
    convert_rate,
    ensure_utc,
    is_fresh,
    market_for_source,
    max_age_for_source,
    rate_age_seconds,
    source_filters,
    supported_pairs,
)

router = APIRouter(prefix="/fx", tags=["fx"])

Market = Literal["any", "official", "parallel"]

PAIR_QUERY = Query(
    "NGN/USD",
    description=(
        "FX pair in either orientation, e.g. 'NGN/USD' or 'USD/NGN'. "
        f"Supported: {', '.join(supported_pairs())}."
    ),
)

MARKET_QUERY = Query(
    MARKET_ANY,
    description=(
        "Which feed to read: 'official' (Fixer.io / Open Exchange Rates), "
        "'parallel' (street rates) or 'any' (most recent of either)."
    ),
)


class FxRateResponse(BaseModel):
    pair: str = Field(..., description="Pair in the orientation that was requested")
    rate: float = Field(..., description="Live rate in the requested orientation")
    timestamp: datetime = Field(..., description="When the source quoted this rate")
    source: str
    market: str = Field(..., description="'official' or 'parallel'")
    age_seconds: float = Field(..., description="How old the quote is, in seconds")
    is_stale: bool = Field(
        ...,
        description="True once the quote is older than its source's freshness "
        f"budget ({MAX_RATE_AGE_MINUTES} minutes for the continuously updating feeds)",
    )


class FxRatePoint(BaseModel):
    timestamp: datetime
    pair: str
    rate: float
    source: str
    market: str


class FxSourceStatus(BaseModel):
    source: str
    market: str
    pair: str
    last_timestamp: datetime
    last_rate: float
    age_seconds: float
    is_stale: bool
    points_last_24h: int


def _resolve_pair(pair):
    """Normalises a requested pair, translating rejections into a 400."""
    try:
        return canonical_pair(pair)
    except UnsupportedPairError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/current", response_model=FxRateResponse)
def get_current_rate(
    pair: str = PAIR_QUERY,
    market: Market = MARKET_QUERY,
    allow_stale: bool = Query(
        False,
        description="Return the last known rate even when it fails the freshness check.",
    ),
):
    """
    Returns the most recent live rate for ``pair``.

    Rates are stored as ``USD/<CCY>``; asking for ``NGN/USD`` returns the
    inverted quote so callers never have to know the storage orientation.

    **Stale-rate guard** — if the newest stored quote is older than
    ``FX_MAX_RATE_AGE_MINUTES`` (45 by default, i.e. three missed ingestion
    cycles) the endpoint responds **503** instead of serving a number that looks
    live but is not. Pass ``allow_stale=true`` to read it anyway; the response
    always carries ``age_seconds`` and ``is_stale``.
    """
    canonical, inverted = _resolve_pair(pair)
    sources, exclude_sources = source_filters(market)

    row = get_latest_fx_rate(
        canonical, sources=sources, exclude_sources=exclude_sources
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No live rates stored for '{canonical}'"
                + (f" on the {market} market" if market != MARKET_ANY else "")
                + ". The ingester may not have run yet."
            ),
        )

    timestamp = ensure_utc(row["timestamp"])
    age = rate_age_seconds(timestamp)
    max_age = max_age_for_source(row["source"])
    stale = not is_fresh(timestamp, max_age_minutes=max_age)

    if stale and not allow_stale:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Stale rate for '{canonical}': last quote from {row['source']} is "
                f"{age / 60:.1f} minutes old (max {max_age}). "
                "Retry once ingestion recovers, or pass allow_stale=true."
            ),
        )

    return FxRateResponse(
        pair=pair.strip().upper(),
        rate=convert_rate(row["rate"], inverted),
        timestamp=timestamp,
        source=row["source"],
        market=market_for_source(row["source"]),
        age_seconds=age,
        is_stale=stale,
    )


@router.get("/history", response_model=list[FxRatePoint])
def get_rate_history(
    pair: str = PAIR_QUERY,
    market: Market = MARKET_QUERY,
    hours: int = Query(
        24, ge=1, le=720, description="How far back to look, in hours (max 30 days)"
    ),
    limit: int = Query(
        500, ge=1, le=5000, description="Maximum number of points to return"
    ),
):
    """
    Returns stored rates for ``pair`` over the last ``hours``, oldest first.

    This is the same history ``GET /risk/current`` scores, so a user can audit
    the numbers behind their volatility score.
    """
    canonical, inverted = _resolve_pair(pair)
    sources, exclude_sources = source_filters(market)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    rows = get_fx_rate_window(
        canonical,
        since,
        sources=sources,
        exclude_sources=exclude_sources,
        limit=limit,
    )
    requested_pair = pair.strip().upper()
    return [
        FxRatePoint(
            timestamp=ensure_utc(row["timestamp"]),
            pair=requested_pair,
            rate=convert_rate(row["rate"], inverted),
            source=row["source"],
            market=market_for_source(row["source"]),
        )
        for row in rows
    ]


@router.get("/sources", response_model=list[FxSourceStatus])
def get_source_health(
    pair: str | None = Query(
        None, description="Optional pair filter, e.g. 'USD/NGN' or 'NGN/USD'"
    ),
):
    """
    Returns ingestion health per (source, pair): the newest quote each feed has
    delivered, its age and whether it currently passes the freshness check.

    Use this to tell "the parallel feed is down" apart from "the whole ingester
    is down" — see ``runbooks/data-source-down.md``.
    """
    canonical = _resolve_pair(pair)[0] if pair else None
    rows = get_fx_source_status(pair=canonical)

    statuses = []
    for row in rows:
        timestamp = ensure_utc(row["last_timestamp"])
        statuses.append(
            FxSourceStatus(
                source=row["source"],
                market=market_for_source(row["source"]),
                pair=row["pair"],
                last_timestamp=timestamp,
                last_rate=float(row["last_rate"]),
                age_seconds=rate_age_seconds(timestamp),
                is_stale=not is_fresh(
                    timestamp, max_age_minutes=max_age_for_source(row["source"])
                ),
                points_last_24h=int(row["points_last_24h"]),
            )
        )
    return statuses
