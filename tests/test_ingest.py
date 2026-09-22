"""Tests for Phase 1 ingest: RSS normalization, dedup, store, replay."""

import json
import hashlib
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
import feedparser

from verticals.ingest.normalize import canonical_url, normalize_entry, dedup_articles
from verticals.ingest.rss import fetch_single_feed, fetch_all_feeds, _safe_name
from verticals.ingest.store import create_run_dir, save_articles, save_sources, replay_from_raw


SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>MacRumors: Mac News and Rumors</title>
<link>https://www.macrumors.com/</link>
<item>
  <title>iPhone 18 Pro Leak Reveals New Chip</title>
  <link>https://www.macrumors.com/2026/09/20/iphone-18-pro-chip-leak/?utm_source=feed&amp;utm_medium=rss</link>
  <guid>https://www.macrumors.com/2026/09/20/iphone-18-pro-chip-leak/</guid>
  <pubDate>Sat, 20 Sep 2026 10:00:00 +0000</pubDate>
  <description><![CDATA[Apple is testing a new 2nm chip for iPhone 18 Pro.]]></description>
</item>
<item>
  <title>WatchOS 12 Released</title>
  <link>https://www.macrumors.com/2026/09/20/watchos-12-released#comments</link>
  <guid>https://www.macrumors.com/2026/09/20/watchos-12-released</guid>
  <pubDate>Sun, 21 Sep 2026 08:00:00 +0000</pubDate>
  <description>watchOS 12 is now available for all supported Apple Watch models.</description>
</item>
</channel></rss>"""

SAMPLE_RSS_DUP = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>9to5Mac</title>
<link>https://9to5mac.com/</link>
<item>
  <title>iPhone 18 Pro Leak Reveals New Chip</title>
  <link>https://www.macrumors.com/2026/09/20/iphone-18-pro-chip-leak/</link>
  <guid>https://www.macrumors.com/2026/09/20/iphone-18-pro-chip-leak/</guid>
  <pubDate>Sat, 20 Sep 2026 10:00:00 +0000</pubDate>
  <description>Same story syndicated on 9to5.</description>
</item>
</channel></rss>"""


class TestCanonicalUrl:
    def test_strips_utm_and_fragment(self):
        url = "https://www.macrumors.com/2026/09/20/iphone-18-pro-chip-leak/?utm_source=feed&utm_medium=rss#comments"
        assert canonical_url(url) == "https://www.macrumors.com/2026/09/20/iphone-18-pro-chip-leak/"

    def test_strips_fbclid(self):
        url = "https://9to5mac.com/2026/09/20/new-mac?fbclid=abc123&foo=bar"
        assert "fbclid" not in canonical_url(url)
        assert "foo=bar" in canonical_url(url)

    def test_preserves_non_tracking(self):
        url = "https://www.macrumors.com/search?query=iphone&page=2"
        assert "query=iphone" in canonical_url(url)


class TestNormalizeEntry:
    def test_basic_fields(self):
        parsed = feedparser.parse(SAMPLE_RSS)
        assert len(parsed.entries) == 2
        art = normalize_entry(parsed.entries[0], "https://feeds.macrumors.com/MacRumors-All", "2026-09-22T05:00:00Z")
        assert art["source"] == "macrumors"
        assert art["source_type"] == "rss"
        assert art["title"] == "iPhone 18 Pro Leak Reveals New Chip"
        assert "canonical_url" in art
        assert "utm" not in art["canonical_url"]
        assert art["published_at"] == "2026-09-20T10:00:00Z"
        assert art["url"] == "https://www.macrumors.com/2026/09/20/iphone-18-pro-chip-leak/?utm_source=feed&utm_medium=rss"
        assert art["article_id"].startswith("rss_")
        assert art["fetched_at"] == "2026-09-22T05:00:00Z"

    def test_published_at_parsed(self):
        parsed = feedparser.parse(SAMPLE_RSS)
        art = normalize_entry(parsed.entries[1], "https://feeds.macrumors.com/MacRumors-All", "2026-09-22T05:00:00Z")
        # second entry 21 Sep
        assert art["published_at"] == "2026-09-21T08:00:00Z"

    def test_strips_html_in_summary(self):
        parsed = feedparser.parse(SAMPLE_RSS)
        art = normalize_entry(parsed.entries[0], "https://feeds.macrumors.com/MacRumors-All", "2026-09-22T05:00:00Z")
        assert "<" not in art["summary"]
        assert "2nm chip" in art["summary"]


class TestDedup:
    def test_dedup_by_canonical(self):
        a1 = {"canonical_url": "https://www.macrumors.com/a", "article_id": "1", "published_at": "2026-09-20T10:00:00Z"}
        a2 = {"canonical_url": "https://www.macrumors.com/a", "article_id": "2", "published_at": "2026-09-21T10:00:00Z"}
        deduped = dedup_articles([a1, a2])
        assert len(deduped) == 1
        assert deduped[0]["published_at"] == "2026-09-21T10:00:00Z"

    def test_keeps_distinct(self):
        a1 = {"canonical_url": "https://a.com/1", "article_id": "1", "published_at": "2026-09-20T10:00:00Z"}
        a2 = {"canonical_url": "https://a.com/2", "article_id": "2", "published_at": "2026-09-20T11:00:00Z"}
        assert len(dedup_articles([a1, a2])) == 2


class TestFetchSingleFeed:
    @patch("verticals.ingest.rss.requests.get")
    def test_ok(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = SAMPLE_RSS
        mock_get.return_value = mock_resp
        res = fetch_single_feed("https://feeds.macrumors.com/MacRumors-All")
        assert res["status"] == "ok"
        assert res["http_status"] == 200
        assert res["entries_fetched"] == 2
        assert res["entries_kept"] == 2
        assert res["raw_hash"] == hashlib.sha256(SAMPLE_RSS).hexdigest()
        assert len(res["articles"]) == 2

    @patch("verticals.ingest.rss.requests.get")
    def test_http_error(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.content = b"error"
        mock_get.return_value = mock_resp
        res = fetch_single_feed("https://example.com/feed")
        assert res["status"] == "error"
        assert "HTTP 500" in res["error"]

    @patch("verticals.ingest.rss.requests.get")
    def test_timeout(self, mock_get):
        import requests as req
        mock_get.side_effect = req.exceptions.Timeout()
        res = fetch_single_feed("https://example.com/feed")
        assert res["status"] == "error"
        assert res["error"] == "timeout"

    @patch("verticals.ingest.rss.requests.get")
    def test_bozo_but_has_entries(self, mock_get):
        bad = b"not xml at all < < <"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = bad
        mock_get.return_value = mock_resp
        res = fetch_single_feed("https://example.com/feed")
        # feedparser will bozo, 0 entries -> empty or error
        assert res["status"] in ("empty", "error")
        assert res["entries_fetched"] == 0


class TestStoreAndReplay:
    def test_create_run_and_save(self, tmp_path):
        run_dir = create_run_dir(tmp_path, "apple")
        assert run_dir.exists()
        assert (run_dir / "raw").exists()
        # save articles
        articles = [
            {"article_id": "rss_abc", "canonical_url": "https://a.com/1", "title": "T1", "source": "macrumors", "published_at": "2026-09-20T10:00:00Z", "url": "https://a.com/1"},
            {"article_id": "rss_def", "canonical_url": "https://a.com/2", "title": "T2", "source": "9to5mac", "published_at": "2026-09-21T10:00:00Z", "url": "https://a.com/2"},
        ]
        save_articles(run_dir, articles)
        j = json.loads((run_dir / "articles.json").read_text())
        assert len(j) == 2
        # sorted newest first
        assert j[0]["title"] == "T2"

    def test_save_sources_per_feed_metrics(self, tmp_path):
        run_dir = create_run_dir(tmp_path, "apple")
        feed_results = [
            {"feed_url": "https://feeds.macrumors.com/MacRumors-All", "status": "ok", "http_status": 200, "bozo": False, "bozo_exception": None, "entries_fetched": 10, "entries_kept": 8, "error": None, "fetched_at": "2026-09-22T05:00:00Z", "duration_ms": 123, "raw_hash": "abc", "raw_path": "raw/foo.xml", "feed_title": "MacRumors"},
            {"feed_url": "https://9to5mac.com/feed/", "status": "error", "http_status": 500, "bozo": False, "bozo_exception": None, "entries_fetched": 0, "entries_kept": 0, "error": "HTTP 500", "fetched_at": "2026-09-22T05:00:01Z", "duration_ms": 50, "raw_hash": None, "raw_path": None, "feed_title": None},
        ]
        save_sources(run_dir, feed_results, niche="apple", limit=20)
        s = json.loads((run_dir / "sources.json").read_text())
        assert s["total_feeds"] == 2
        assert s["feeds_ok"] == 1
        assert s["feeds_error"] == 1
        assert len(s["feeds"]) == 2
        assert s["feeds"][0]["url"] == "https://feeds.macrumors.com/MacRumors-All"
        assert s["feeds"][0]["raw_hash"] == "abc"
        assert s["feeds"][1]["error"] == "HTTP 500"
        # per-feed status preserved
        assert s["feeds"][1]["status"] == "error"
        # meta.json
        assert (run_dir / "meta.json").exists()

    def test_replay_offline(self, tmp_path):
        # Simulate a run with raw files + sources.json
        run_dir = create_run_dir(tmp_path, "apple")
        # write two raw files
        name1 = _safe_name("https://feeds.macrumors.com/MacRumors-All")
        name2 = _safe_name("https://9to5mac.com/feed/")
        (run_dir / "raw" / f"{name1}.xml").write_bytes(SAMPLE_RSS)
        (run_dir / "raw" / f"{name2}.xml").write_bytes(SAMPLE_RSS_DUP)
        # sources.json mapping
        import json
        sources = {
            "feeds": [
                {"url": "https://feeds.macrumors.com/MacRumors-All", "raw_path": f"raw/{name1}.xml"},
                {"url": "https://9to5mac.com/feed/", "raw_path": f"raw/{name2}.xml"},
            ]
        }
        (run_dir / "sources.json").write_text(json.dumps(sources))
        # replay
        articles = replay_from_raw(run_dir)
        # SAMPLE_RSS has 2, SAMPLE_RSS_DUP has 1 (dup url), total 3 parsed, but dup detection not done in replay (only dedup later)
        assert len(articles) >= 3
        # check one article from each
        titles = [a["title"] for a in articles]
        assert "iPhone 18 Pro Leak Reveals New Chip" in titles
        assert "WatchOS 12 Released" in titles

    @patch("verticals.ingest.rss.requests.get")
    def test_fetch_all_writes_raw(self, mock_get, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = SAMPLE_RSS
        mock_get.return_value = mock_resp
        run_dir = create_run_dir(tmp_path, "apple")
        results = fetch_all_feeds(["https://feeds.macrumors.com/MacRumors-All"], run_dir=run_dir)
        assert results[0]["raw_path"] is not None
        raw_file = run_dir / results[0]["raw_path"]
        assert raw_file.exists()
        assert raw_file.read_bytes() == SAMPLE_RSS
