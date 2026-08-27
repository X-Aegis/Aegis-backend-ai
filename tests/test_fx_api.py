"""Tests for GET /fx/current, /fx/history and /fx/sources."""

import os
import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.main import app

client = TestClient(app)


def _rate_row(rate=1750.0, source="Fixer.io", pair="USD/NGN", age_minutes=1):
    return {
        "timestamp": datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        "pair": pair,
        "rate": rate,
        "source": source,
    }


# ---------------------------------------------------------------------------
# GET /fx/current
# ---------------------------------------------------------------------------


def test_current_rate_returns_the_latest_live_quote(monkeypatch):
    monkeypatch.setattr(
        "api.fx.get_latest_fx_rate",
        lambda pair, sources=None, exclude_sources=None: _rate_row(),
    )

    response = client.get("/fx/current", params={"pair": "USD/NGN"})

    assert response.status_code == 200
    data = response.json()
    assert data["pair"] == "USD/NGN"
    assert data["rate"] == 1750.0
    assert data["source"] == "Fixer.io"
    assert data["market"] == "official"
    assert data["is_stale"] is False
    assert data["age_seconds"] > 0


def test_current_rate_inverts_when_asked_for_ngn_usd(monkeypatch):
    """The issue's example: GET /fx/current?pair=NGN/USD."""
    monkeypatch.setattr(
        "api.fx.get_latest_fx_rate",
        lambda pair, sources=None, exclude_sources=None: _rate_row(rate=1750.0),
    )

    response = client.get("/fx/current", params={"pair": "NGN/USD"})

    assert response.status_code == 200
    data = response.json()
    assert data["pair"] == "NGN/USD"
    assert data["rate"] == 1 / 1750.0


def test_current_rate_queries_the_canonical_pair(monkeypatch):
    captured = {}

    def fake_latest(pair, sources=None, exclude_sources=None):
        captured.update(pair=pair, sources=sources, exclude_sources=exclude_sources)
        return _rate_row(pair="USD/KES", rate=132.5)

    monkeypatch.setattr("api.fx.get_latest_fx_rate", fake_latest)

    response = client.get("/fx/current", params={"pair": "kes/usd"})

    assert response.status_code == 200
    assert captured["pair"] == "USD/KES"
    assert captured["sources"] is None and captured["exclude_sources"] is None


def test_current_rate_market_filter_selects_parallel_feeds(monkeypatch):
    captured = {}

    def fake_latest(pair, sources=None, exclude_sources=None):
        captured.update(sources=sources, exclude_sources=exclude_sources)
        return _rate_row(rate=1815.0, source="ParallelMarket")

    monkeypatch.setattr("api.fx.get_latest_fx_rate", fake_latest)

    response = client.get(
        "/fx/current", params={"pair": "USD/NGN", "market": "parallel"}
    )

    assert response.status_code == 200
    assert response.json()["market"] == "parallel"
    assert captured["sources"] is None
    assert "Fixer.io" in captured["exclude_sources"]


def test_current_rate_market_filter_selects_official_feeds(monkeypatch):
    captured = {}

    def fake_latest(pair, sources=None, exclude_sources=None):
        captured.update(sources=sources, exclude_sources=exclude_sources)
        return _rate_row()

    monkeypatch.setattr("api.fx.get_latest_fx_rate", fake_latest)

    response = client.get(
        "/fx/current", params={"pair": "USD/NGN", "market": "official"}
    )

    assert response.status_code == 200
    assert "Fixer.io" in captured["sources"]
    assert captured["exclude_sources"] is None


def test_current_rate_rejects_an_unknown_market():
    response = client.get("/fx/current", params={"market": "moon"})

    assert response.status_code == 422


def test_current_rate_returns_404_when_nothing_ingested(monkeypatch):
    monkeypatch.setattr(
        "api.fx.get_latest_fx_rate",
        lambda pair, sources=None, exclude_sources=None: None,
    )

    response = client.get("/fx/current", params={"pair": "USD/NGN"})

    assert response.status_code == 404
    assert "USD/NGN" in response.json()["detail"]


def test_current_rate_rejects_unsupported_pairs(monkeypatch):
    response = client.get("/fx/current", params={"pair": "EUR/GBP"})

    assert response.status_code == 400
    assert "Unsupported pair" in response.json()["detail"]


def test_current_rate_stale_guard_returns_503(monkeypatch):
    """A quote older than the freshness window is withheld, not served."""
    monkeypatch.setattr(
        "api.fx.get_latest_fx_rate",
        lambda pair, sources=None, exclude_sources=None: _rate_row(age_minutes=180),
    )

    response = client.get("/fx/current", params={"pair": "USD/NGN"})

    assert response.status_code == 503
    assert "Stale rate" in response.json()["detail"]


def test_current_rate_stale_guard_can_be_bypassed_explicitly(monkeypatch):
    monkeypatch.setattr(
        "api.fx.get_latest_fx_rate",
        lambda pair, sources=None, exclude_sources=None: _rate_row(age_minutes=180),
    )

    response = client.get(
        "/fx/current", params={"pair": "USD/NGN", "allow_stale": "true"}
    )

    assert response.status_code == 200
    data = response.json()
    assert data["is_stale"] is True
    assert data["age_seconds"] > 3600


# ---------------------------------------------------------------------------
# GET /fx/history
# ---------------------------------------------------------------------------


def test_history_returns_points_oldest_first(monkeypatch):
    rows = [
        _rate_row(rate=1740.0, age_minutes=30),
        _rate_row(rate=1745.0, age_minutes=15),
        _rate_row(rate=1750.0, age_minutes=1),
    ]
    monkeypatch.setattr(
        "api.fx.get_fx_rate_window",
        lambda pair, since, sources=None, exclude_sources=None, limit=None: rows,
    )

    response = client.get("/fx/history", params={"pair": "USD/NGN"})

    assert response.status_code == 200
    data = response.json()
    assert [point["rate"] for point in data] == [1740.0, 1745.0, 1750.0]
    assert all(point["market"] == "official" for point in data)


def test_history_inverts_rates_for_the_requested_orientation(monkeypatch):
    monkeypatch.setattr(
        "api.fx.get_fx_rate_window",
        lambda pair, since, sources=None, exclude_sources=None, limit=None: [
            _rate_row(rate=1750.0)
        ],
    )

    response = client.get("/fx/history", params={"pair": "NGN/USD"})

    assert response.status_code == 200
    assert response.json()[0]["rate"] == 1 / 1750.0


def test_history_passes_window_and_limit(monkeypatch):
    captured = {}

    def fake_window(pair, since, sources=None, exclude_sources=None, limit=None):
        captured.update(pair=pair, since=since, limit=limit)
        return []

    monkeypatch.setattr("api.fx.get_fx_rate_window", fake_window)

    response = client.get(
        "/fx/history", params={"pair": "USD/KES", "hours": 6, "limit": 42}
    )

    assert response.status_code == 200
    assert response.json() == []
    assert captured["pair"] == "USD/KES"
    assert captured["limit"] == 42
    age_hours = (datetime.now(timezone.utc) - captured["since"]).total_seconds() / 3600
    assert 5.9 < age_hours < 6.1


def test_history_rejects_an_out_of_range_window():
    assert client.get("/fx/history", params={"hours": 0}).status_code == 422
    assert client.get("/fx/history", params={"hours": 10_000}).status_code == 422


# ---------------------------------------------------------------------------
# GET /fx/sources
# ---------------------------------------------------------------------------


def test_sources_reports_per_feed_freshness(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        {
            "source": "Fixer.io",
            "pair": "USD/NGN",
            "last_timestamp": now - timedelta(minutes=3),
            "last_rate": 1750.0,
            "points_last_24h": 96,
        },
        {
            "source": "ParallelMarket",
            "pair": "USD/NGN",
            "last_timestamp": now - timedelta(hours=5),
            "last_rate": 1815.0,
            "points_last_24h": 12,
        },
    ]
    monkeypatch.setattr("api.fx.get_fx_source_status", lambda pair=None: rows)

    response = client.get("/fx/sources")

    assert response.status_code == 200
    data = response.json()
    assert data[0]["market"] == "official"
    assert data[0]["is_stale"] is False
    assert data[1]["market"] == "parallel"
    assert data[1]["is_stale"] is True
    assert data[1]["points_last_24h"] == 12


def test_sources_filters_by_canonical_pair(monkeypatch):
    captured = {}

    def fake_status(pair=None):
        captured["pair"] = pair
        return []

    monkeypatch.setattr("api.fx.get_fx_source_status", fake_status)

    response = client.get("/fx/sources", params={"pair": "NGN/USD"})

    assert response.status_code == 200
    assert captured["pair"] == "USD/NGN"
