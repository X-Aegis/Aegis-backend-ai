import asyncio
import os
import sys
from datetime import datetime, timezone

import feedparser
import httpx
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Add project root to sys.path for local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.database import save_sentiment_data

TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")

# Keywords used to filter relevant social/news content
KEYWORDS = ["Naira", "devaluation", "CBN", "inflation"]

# Financial news RSS feeds to poll for sentiment signals
NEWS_RSS_FEEDS = [
    ("Nairametrics", "https://nairametrics.com/feed/"),
    ("Punch Business", "https://punchng.com/topics/business/feed/"),
    (
        "Reuters Africa",
        "https://www.reutersagency.com/feed/?best-regions=africa&post_type=best",
    ),
]

_analyzer = SentimentIntensityAnalyzer()


def _score_text(text):
    """Returns a compound sentiment score in the range [-1, 1]."""
    return _analyzer.polarity_scores(text)["compound"]


def _matched_keywords(text):
    """Returns the subset of target KEYWORDS present in text (case-insensitive)."""
    lowered = text.lower()
    return [keyword for keyword in KEYWORDS if keyword.lower() in lowered]


async def fetch_twitter_sentiment():
    """Fetches recent Twitter/X posts mentioning target keywords and scores their sentiment."""
    if not TWITTER_BEARER_TOKEN:
        print("Twitter/X bearer token not found.")
        return []

    query = "(" + " OR ".join(KEYWORDS) + ") lang:en -is:retweet"
    url = "https://api.twitter.com/2/tweets/search/recent"
    headers = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}
    params = {"query": query, "max_results": 50, "tweet.fields": "created_at"}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=params)
            data = response.json()

            if "data" not in data:
                print(f"Twitter/X API returned no data: {data.get('errors', data)}")
                return []

            records = []
            for tweet in data["data"]:
                text = tweet.get("text", "")
                keywords = _matched_keywords(text)
                if not keywords:
                    continue

                created_at = tweet.get("created_at")
                if created_at:
                    timestamp = datetime.strptime(
                        created_at, "%Y-%m-%dT%H:%M:%S.%fZ"
                    ).replace(tzinfo=timezone.utc)
                else:
                    timestamp = datetime.now(timezone.utc)

                score = _score_text(text)
                for keyword in keywords:
                    records.append((timestamp, "Twitter/X", keyword, text, score))
            return records
    except Exception as e:  # noqa: BLE001
        print(f"Error fetching from Twitter/X: {e}")
        return []


async def fetch_news_sentiment():
    """Fetches financial news RSS entries mentioning target keywords and scores their sentiment."""
    records = []
    for source_name, feed_url in NEWS_RSS_FEEDS:
        try:
            feed = await asyncio.to_thread(feedparser.parse, feed_url)
            for entry in feed.entries:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                text = f"{title}. {summary}".strip()

                keywords = _matched_keywords(text)
                if not keywords:
                    continue

                published = entry.get("published_parsed")
                if published:
                    timestamp = datetime(*published[:6], tzinfo=timezone.utc)
                else:
                    timestamp = datetime.now(timezone.utc)

                score = _score_text(text)
                for keyword in keywords:
                    records.append((timestamp, source_name, keyword, text, score))
        except Exception as e:  # noqa: BLE001
            print(f"Error fetching from {source_name}: {e}")

    return records


async def run_ingestion():
    """Orchestrates the sentiment ingestion process."""
    print(f"Starting sentiment data ingestion at {datetime.now(timezone.utc)}")

    tasks = [
        fetch_twitter_sentiment(),
        fetch_news_sentiment(),
    ]

    results = await asyncio.gather(*tasks)
    all_records = [record for result in results for record in result]

    if all_records:
        print(f"Fetched {len(all_records)} sentiment records. Saving to database...")
        save_sentiment_data(all_records)
        print("Ingestion successful.")
    else:
        print("No sentiment records fetched.")


if __name__ == "__main__":
    asyncio.run(run_ingestion())
