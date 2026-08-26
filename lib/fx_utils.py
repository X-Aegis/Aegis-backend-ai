"""lib/fx_utils.py

Shared helpers for FX pair handling and rate freshness.

Rates are stored in ``fx_rates`` in a single canonical orientation —
``USD/<CCY>``, i.e. *units of the local currency per 1 USD* (e.g. ``USD/NGN``
= 1750.0). Callers may ask for either orientation (``NGN/USD``), so pairs are
normalised here and the rate inverted when the caller wants the mirror side.

Sources are split into two markets:

- ``official``  — regulated/interbank vendor APIs (Fixer.io, Open Exchange Rates)
- ``parallel``  — parallel ("black market") street rates, which is what people
  in Lagos or Nairobi actually transact at

Anything that is not a known official vendor is treated as a parallel feed, so
adding a new street-rate source needs no change here.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone

BASE_CURRENCY = "USD"

# Currencies the ingester tracks. NGN and KES are the product's launch markets.
SUPPORTED_CURRENCIES = ("NGN", "KES", "GHS", "ZAR")

# Vendor APIs that publish official/interbank rates. ExchangeRate-API is the
# keyless last-resort fallback, so the product still serves real rates on a
# deployment that has no vendor credentials configured yet.
OFFICIAL_SOURCES = ("Fixer.io", "OpenExchangeRates", "ExchangeRate-API")

MARKET_OFFICIAL = "official"
MARKET_PARALLEL = "parallel"
MARKET_ANY = "any"

# A rate older than this is considered stale. Ingestion runs every 15 minutes,
# so this tolerates three consecutive missed cycles before we stop trusting it.
MAX_RATE_AGE_MINUTES = int(os.getenv("FX_MAX_RATE_AGE_MINUTES", "45"))

# Clock skew we tolerate on a source that reports a timestamp in the future.
MAX_CLOCK_SKEW_MINUTES = int(os.getenv("FX_MAX_CLOCK_SKEW_MINUTES", "5"))

# Feeds that publish less often than we poll get their own freshness budget —
# holding a once-a-day feed to a 45-minute rule would discard every quote it
# ever publishes. Anything not listed here uses MAX_RATE_AGE_MINUTES.
SOURCE_MAX_AGE_MINUTES = {
    # The free ExchangeRate-API tier refreshes once every 24 hours.
    "ExchangeRate-API": int(os.getenv("FX_DAILY_SOURCE_MAX_AGE_MINUTES", "1500")),
}


class UnsupportedPairError(ValueError):
    """Raised when a caller asks for a pair the ingester does not track."""


def supported_pairs():
    """Returns the canonical pairs stored in ``fx_rates``."""
    return tuple(f"{BASE_CURRENCY}/{ccy}" for ccy in SUPPORTED_CURRENCIES)


def parse_pair(pair):
    """
    Splits ``"USD/NGN"`` into ``("USD", "NGN")``.

    Raises :class:`UnsupportedPairError` if the string is not a well formed
    ``BASE/QUOTE`` pair of three-letter currency codes.
    """
    if not isinstance(pair, str):
        raise UnsupportedPairError(f"Invalid pair: {pair!r}")

    parts = pair.strip().upper().split("/")
    if len(parts) != 2 or not all(p.isalpha() and len(p) == 3 for p in parts):
        raise UnsupportedPairError(
            f"Invalid pair {pair!r}. Expected the form 'USD/NGN' or 'NGN/USD'."
        )
    return parts[0], parts[1]


def canonical_pair(pair):
    """
    Maps any supported orientation onto the stored ``USD/<CCY>`` pair.

    Returns ``(canonical_pair, inverted)`` where ``inverted`` is True when the
    caller asked for ``<CCY>/USD`` and the stored rate has to be flipped.

    >>> canonical_pair("NGN/USD")
    ('USD/NGN', True)
    >>> canonical_pair("USD/NGN")
    ('USD/NGN', False)
    """
    base, quote = parse_pair(pair)

    if base == BASE_CURRENCY and quote in SUPPORTED_CURRENCIES:
        return f"{BASE_CURRENCY}/{quote}", False
    if quote == BASE_CURRENCY and base in SUPPORTED_CURRENCIES:
        return f"{BASE_CURRENCY}/{base}", True

    raise UnsupportedPairError(
        f"Unsupported pair '{base}/{quote}'. Supported pairs: "
        + ", ".join(supported_pairs())
        + " (either orientation)."
    )


def convert_rate(rate, inverted):
    """Returns the rate in the orientation the caller asked for."""
    rate = float(rate)
    if not inverted:
        return rate
    if rate <= 0:
        raise ValueError("Cannot invert a non-positive rate.")
    return 1.0 / rate


def market_for_source(source):
    """Classifies a source name as an ``official`` or ``parallel`` market feed."""
    return MARKET_OFFICIAL if source in OFFICIAL_SOURCES else MARKET_PARALLEL


def source_filters(market):
    """
    Translates a ``market`` selector into ``(sources, exclude_sources)`` filters
    for the ``fx_rates`` queries.

    ``parallel`` cannot be expressed as a fixed allow-list (any feed that is not
    a known official vendor counts), so it is expressed as an exclusion instead.
    """
    if market == MARKET_OFFICIAL:
        return list(OFFICIAL_SOURCES), None
    if market == MARKET_PARALLEL:
        return None, list(OFFICIAL_SOURCES)
    return None, None


def max_age_for_source(source):
    """Returns the freshness budget (minutes) a given feed is held to."""
    return SOURCE_MAX_AGE_MINUTES.get(source, MAX_RATE_AGE_MINUTES)


def ensure_utc(timestamp):
    """Coerces a naive datetime to UTC; tz-aware values pass through unchanged."""
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def rate_age_seconds(timestamp, now=None):
    """Returns how old a rate is, in seconds (negative if it is in the future)."""
    now = now or datetime.now(timezone.utc)
    return (ensure_utc(now) - ensure_utc(timestamp)).total_seconds()


def is_fresh(timestamp, now=None, max_age_minutes=None):
    """
    True when a rate timestamp is recent enough to be trusted.

    A timestamp further in the future than ``MAX_CLOCK_SKEW_MINUTES`` is also
    rejected — a source with a broken clock is not a source we can price off.
    """
    max_age_minutes = (
        MAX_RATE_AGE_MINUTES if max_age_minutes is None else max_age_minutes
    )
    age = rate_age_seconds(timestamp, now=now)
    if age < -timedelta(minutes=MAX_CLOCK_SKEW_MINUTES).total_seconds():
        return False
    return age <= timedelta(minutes=max_age_minutes).total_seconds()


def is_valid_rate(rate):
    """True when a quoted rate is a usable positive, finite number."""
    try:
        value = float(rate)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and value > 0
