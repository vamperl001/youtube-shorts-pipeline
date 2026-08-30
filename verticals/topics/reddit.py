"""Reddit RSS topic source (hot/trending) — uses the RSS API instead of .json.

Reddit blocks server/datacenter .json API requests with HTTP 403. The RSS
endpoint (www.reddit.com/r/<sub>/.rss) works but is rate-limited (HTTP 429
after the first request), so the result may be empty when throttled.
"""

import feedparser

from .base import TopicCandidate, TopicSource


class RedditSource(TopicSource):
    name = "reddit"

    def __init__(self, config: dict = None):
        config = config or {}
        self.subreddits = config.get("subreddits", ["technology", "worldnews"])

    @property
    def is_available(self) -> bool:
        try:
            import feedparser  # noqa: F401
            return True
        except ImportError:
            return False

    def fetch_topics(self, limit: int = 10) -> list[TopicCandidate]:
        topics = []
        per_sub = max(1, limit // len(self.subreddits))

        for sub in self.subreddits:
            try:
                topics.extend(self._fetch_subreddit(sub, per_sub))
            except Exception:
                continue

        return topics[:limit]

    def _fetch_subreddit(self, subreddit: str, limit: int) -> list[TopicCandidate]:
        url = f"https://www.reddit.com/r/{subreddit}/.rss"
        feed = feedparser.parse(url)
        # feedparser swallows HTTP errors; a non-200/429/403/empty feed yields no entries
        if feed.bozo or not feed.entries:
            return []

        topics = []
        for entry in feed.entries[:limit]:
            title = entry.get("title", "").strip()
            if not title:
                continue

            link = entry.get("link", "")
            summary = entry.get("summary", "")
            # Reddit RSS summaries carry author info, strip HTML tags
            import re
            summary_text = re.sub(r"<[^>]+>", "", summary)[:200]

            topics.append(TopicCandidate(
                title=title,
                source=f"reddit/r/{subreddit}",
                trending_score=0.5,
                summary=summary_text,
                url=link,
                metadata={"feed": url},
            ))

        return topics
