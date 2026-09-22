"""Tests for Phase 2 Reddit ingest — non-blocking community signal."""

from unittest.mock import patch, MagicMock
import json
from pathlib import Path

from verticals.ingest.reddit import fetch_single_subreddit, fetch_reddit_signals
from verticals.ingest.store import save_community

SAMPLE_REDDIT_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>r/apple</title>
<entry><title>Apple Music Hall unveiled</title><link href="https://www.reddit.com/r/apple/comments/abc/test1"/></entry>
<entry><title>M6 Mac mini rumour</title><link href="https://www.reddit.com/r/apple/comments/def/test2"/></entry>
</feed>"""

class TestFetchSingleSubreddit:
    @patch("verticals.ingest.reddit.requests.get")
    def test_ok(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = SAMPLE_REDDIT_RSS
        mock_get.return_value = mock_resp
        res = fetch_single_subreddit("apple")
        assert res["status"] == "ok"
        assert res["signals_fetched"] == 2
        assert res["signals"][0]["title"] == "Apple Music Hall unveiled"
        assert res["signals"][0]["source_quality"] == "community"
        assert res["signals"][0]["status"] == "community"

    @patch("verticals.ingest.reddit.requests.get")
    def test_rate_limited(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.content = b""
        mock_get.return_value = mock_resp
        res = fetch_single_subreddit("apple")
        assert res["status"] == "rate_limited"
        assert res["signals_fetched"] == 0

    @patch("verticals.ingest.reddit.requests.get")
    def test_timeout_non_blocking(self, mock_get):
        import requests as req
        mock_get.side_effect = req.exceptions.Timeout()
        res = fetch_single_subreddit("iphone")
        assert res["status"] == "error"
        assert res["error"] == "timeout"

    @patch("verticals.ingest.reddit.requests.get")
    def test_empty_feed(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>r/iphone</title></feed>'
        mock_get.return_value = mock_resp
        res = fetch_single_subreddit("iphone")
        assert res["status"] == "empty"
        assert res["signals_fetched"] == 0

class TestFetchRedditSignals:
    @patch("verticals.ingest.reddit.fetch_single_subreddit")
    def test_mixed_non_blocking(self, mock_fetch):
        # one ok, one empty, one rate_limited — pipeline continues
        mock_fetch.side_effect = [
            {"subreddit": "apple", "status": "ok", "signals_fetched": 2, "signals": [{"title": "t1"}, {"title": "t2"}], "raw_bytes": b"a", "raw_hash": "h1", "http_status": 200, "bozo": False, "error": None, "fetched_at": "2026-09-22T00:00:00Z", "feed_url": "x", "duration_ms": 10},
            {"subreddit": "iphone", "status": "empty", "signals_fetched": 0, "signals": [], "raw_bytes": b"b", "raw_hash": "h2", "http_status": 200, "bozo": True, "error": None, "fetched_at": "2026-09-22T00:00:00Z", "feed_url": "y", "duration_ms": 10},
        ]
        res = fetch_reddit_signals(["apple", "iphone"], reddit_limit=5, run_dir=None)
        assert len(res) == 2
        assert res[0]["status"] == "ok"
        assert res[1]["status"] == "empty"

class TestSaveCommunity:
    def test_save_community_metrics(self, tmp_path):
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        (run_dir / "meta.json").write_text("{}")
        reddit_results = [
            {"subreddit": "apple", "status": "ok", "signals_fetched": 2, "http_status": 200, "bozo": False, "error": None, "fetched_at": "2026-09-22T00:00:00Z", "duration_ms": 100, "raw_hash": "h1", "raw_path": "raw/a.xml", "feed_url": "https://reddit.com/r/apple/hot.rss"},
            {"subreddit": "iphone", "status": "rate_limited", "signals_fetched": 0, "http_status": 429, "bozo": False, "error": "HTTP 429", "fetched_at": "2026-09-22T00:00:01Z", "duration_ms": 100, "raw_hash": None, "raw_path": None, "feed_url": "https://reddit.com/r/iphone/hot.rss"},
        ]
        signals = [{"title": "t1", "subreddit": "apple"}, {"title": "t2", "subreddit": "apple"}]
        save_community(run_dir, reddit_results, signals, niche="apple")
        j = json.loads((run_dir / "community.json").read_text())
        assert j["signals_count"] == 2
        assert j["reddit_success"] is True
        assert j["reddit_failure"] is False
        assert j["reddit"]["metrics"]["subreddits_rate_limited"] == 1
        assert j["reddit"]["metrics"]["subreddits_ok"] == 1
        # meta patched
        meta = json.loads((run_dir / "meta.json").read_text())
        assert "reddit" in meta
