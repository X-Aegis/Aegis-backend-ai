import asyncio
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.sentiment_ingester import (
    _matched_keywords,
    _score_text,
    fetch_news_sentiment,
    fetch_twitter_sentiment,
)


def test_matched_keywords_finds_case_insensitive_matches():
    text = "The naira continues to slide amid fears of further devaluation."
    matched = _matched_keywords(text)
    assert "Naira" in matched
    assert "devaluation" in matched


def test_matched_keywords_returns_empty_when_no_match():
    assert _matched_keywords("Completely unrelated sports news.") == []


def test_score_text_positive_and_negative():
    positive = _score_text("The Naira rallied strongly and investors are optimistic.")
    negative = _score_text(
        "The Naira crashed and inflation fears are terrifying markets."
    )
    assert positive > 0
    assert negative < 0


def test_fetch_twitter_sentiment_without_token_returns_empty(monkeypatch):
    monkeypatch.setattr("services.sentiment_ingester.TWITTER_BEARER_TOKEN", None)
    assert asyncio.run(fetch_twitter_sentiment()) == []


def test_fetch_news_sentiment_filters_unmatched_entries(monkeypatch):
    class FakeFeed:
        def __init__(self):
            self.entries = [
                {
                    "title": "Naira weakens further amid CBN policy shift",
                    "summary": "Analysts warn of continued devaluation.",
                },
                {
                    "title": "Local football league kicks off",
                    "summary": "A new season begins.",
                },
            ]

    monkeypatch.setattr(
        "services.sentiment_ingester.NEWS_RSS_FEEDS",
        [("TestSource", "http://example.com/feed")],
    )
    monkeypatch.setattr(
        "services.sentiment_ingester.feedparser.parse", lambda url: FakeFeed()
    )

    records = asyncio.run(fetch_news_sentiment())

    assert len(records) >= 1
    for timestamp, source, keyword, content, score in records:
        assert isinstance(timestamp, datetime)
        assert timestamp.tzinfo is not None
        assert source == "TestSource"
        assert keyword in content or keyword.lower() in content.lower()
        assert -1.0 <= score <= 1.0
