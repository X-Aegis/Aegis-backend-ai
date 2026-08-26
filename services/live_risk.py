"""services/live_risk.py

Turns *live* ``fx_rates`` history into the volatility score served by
``GET /risk/current``.

The score is realized volatility, not a stub: it is the standard deviation of
log returns over the requested window, rescaled to the prediction horizon and
mapped onto the product's 0-100 risk scale.

Two details matter for honesty:

* **One source per series.** Official and parallel-market quotes for NGN can sit
  20%+ apart. Interleaving them would manufacture volatility that never
  happened, so :func:`select_rate_series` picks a single feed — preferring the
  parallel market, because that is the rate a Lagos or Nairobi user is exposed
  to — and computes returns within it.
* **Irregular sampling.** Ingestion runs every 15 minutes but cycles can be
  missed, so the sample interval is measured from the data (median gap) rather
  than assumed.
"""

from __future__ import annotations

import math
from itertools import pairwise
from statistics import median

from lib.fx_utils import (
    MARKET_PARALLEL,
    ensure_utc,
    is_valid_rate,
    market_for_source,
)

# Minimum number of live observations before a score is meaningful.
MIN_DATA_POINTS = 8

# Default lookback used when computing realized volatility.
DEFAULT_WINDOW_HOURS = 24

# Volatility (as a fraction) that maps to a score of ~63. With this calibration
# a 3.2% expected move over the horizon crosses the HIGH threshold (score 80).
VOLATILITY_SCALE = 0.02


class InsufficientRateHistory(Exception):
    """Raised when the stored live history cannot support a volatility score."""


def _clean_points(rows):
    """Returns [(timestamp, rate), ...] sorted oldest-first, dropping bad quotes."""
    points = []
    for row in rows:
        rate = row["rate"]
        if not is_valid_rate(rate):
            continue
        points.append((ensure_utc(row["timestamp"]), float(rate)))
    points.sort(key=lambda point: point[0])
    return points


def select_rate_series(rows, min_points=MIN_DATA_POINTS):
    """
    Groups rows by source and returns the series to price off as
    ``{"source", "market", "points"}``.

    Preference order:

    1. A parallel-market feed with at least ``min_points`` observations — this
       is the rate users actually transact at.
    2. Otherwise the source with the most observations (ties broken by the most
       recent observation).

    Returns None when ``rows`` contains no usable quote at all.
    """
    by_source = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row)

    candidates = []
    for source, source_rows in by_source.items():
        points = _clean_points(source_rows)
        if not points:
            continue
        candidates.append(
            {
                "source": source,
                "market": market_for_source(source),
                "points": points,
            }
        )

    if not candidates:
        return None

    parallel = [
        candidate
        for candidate in candidates
        if candidate["market"] == MARKET_PARALLEL
        and len(candidate["points"]) >= min_points
    ]
    pool = parallel or candidates

    return max(pool, key=lambda c: (len(c["points"]), c["points"][-1][0]))


def sample_interval_seconds(points):
    """Median gap between consecutive observations, in seconds."""
    gaps = [
        (later[0] - earlier[0]).total_seconds()
        for earlier, later in pairwise(points)
        if (later[0] - earlier[0]).total_seconds() > 0
    ]
    if not gaps:
        return None
    return median(gaps)


def realized_volatility(points):
    """
    Standard deviation of log returns between consecutive observations
    (per sample, not annualised).
    """
    returns = [
        math.log(later[1] / earlier[1])
        for earlier, later in pairwise(points)
        if earlier[1] > 0 and later[1] > 0
    ]
    if len(returns) < 2:
        raise InsufficientRateHistory(
            "At least three usable rate observations are required."
        )

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance)


def scale_to_horizon(sigma_per_sample, interval_seconds, horizon_hours):
    """
    Rescales a per-sample volatility to the prediction horizon using the
    square-root-of-time rule.
    """
    if not interval_seconds or interval_seconds <= 0:
        raise InsufficientRateHistory(
            "Cannot determine the sampling interval of the rate history."
        )
    samples_in_horizon = (horizon_hours * 3600.0) / interval_seconds
    return sigma_per_sample * math.sqrt(max(samples_in_horizon, 0.0))


def volatility_score(sigma_horizon, scale=VOLATILITY_SCALE):
    """
    Maps a horizon volatility (as a fraction, e.g. 0.02 = 2%) onto the product's
    0-100 risk scale. The mapping saturates, so extreme prints stay bounded:

    - 0.5% move → ~22
    - 2.0% move → ~63
    - 3.2% move → ~80 (HIGH)
    """
    if sigma_horizon <= 0:
        return 0.0
    score = 100.0 * (1.0 - math.exp(-sigma_horizon / scale))
    return round(min(max(score, 0.0), 100.0), 2)


def compute_live_risk(rows, horizon=1, min_points=MIN_DATA_POINTS):
    """
    Computes the live volatility score from raw ``fx_rates`` rows.

    ``rows`` are dicts with timestamp/pair/rate/source keys, as returned by
    :func:`lib.database.get_fx_rate_window`.

    Raises :class:`InsufficientRateHistory` when there is not enough live data
    to produce an honest score — callers surface that as a 503 rather than
    inventing a number.
    """
    series = select_rate_series(rows, min_points=min_points)
    if series is None:
        raise InsufficientRateHistory("No usable live rates in the requested window.")

    points = series["points"]
    if len(points) < min_points:
        raise InsufficientRateHistory(
            f"Only {len(points)} live observation(s) from {series['source']}; "
            f"{min_points} are required."
        )

    interval = sample_interval_seconds(points)
    sigma_sample = realized_volatility(points)
    sigma_horizon = scale_to_horizon(sigma_sample, interval, horizon)

    latest_timestamp, latest_rate = points[-1]
    return {
        "volatility_score": volatility_score(sigma_horizon),
        "source": series["source"],
        "market": series["market"],
        "data_points": len(points),
        "latest_rate": latest_rate,
        "latest_timestamp": latest_timestamp,
        "sample_interval_seconds": interval,
        "realized_volatility": sigma_horizon,
        "window_start": points[0][0],
    }
