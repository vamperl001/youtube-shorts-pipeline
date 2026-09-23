"""Tests for Phase 6 Asset Resolver."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from verticals.asset_resolver import _extract_og_image, _apple_product_url, resolve_assets


SAMPLE_HTML_APPLE = '<html><head><meta property="og:image" content="https://www.apple.com/v/iphone/home/og.jpg"/></head></html>'
SAMPLE_HTML_9TO5 = '<html><head><meta property="og:image" content="https://9to5mac.com/wp-content/og.jpg"/></head></html>'

def test_extract_og():
    assert _extract_og_image(SAMPLE_HTML_APPLE, "https://www.apple.com/iphone/") == "https://www.apple.com/v/iphone/home/og.jpg"
    assert _extract_og_image(SAMPLE_HTML_9TO5, "https://9to5mac.com/2026/09/22/test") == "https://9to5mac.com/wp-content/og.jpg"
    assert _extract_og_image("<html></html>", "https://example.com") is None

def test_apple_url():
    assert "iphone" in _apple_product_url("iPhone 18 Pro camera")
    assert "mac-mini" in _apple_product_url("Mac mini")
    assert _apple_product_url("random") == "https://www.apple.com/"

def test_resolve_found_and_synthetic(tmp_path):
    # mock requests.get for og:image and image download
    editorial = {
        "stories": [
            {"story_id": "s_01", "headline": "iPhone leak", "sources": ["rss_a1"]},
        ]
    }
    articles = [
        {"article_id": "rss_a1", "title": "iPhone leak", "url": "https://www.macrumors.com/2026/09/22/iphone-leak", "source": "macrumors"},
    ]
    shots = {
        "edition": "2026-09-22",
        "shots": [
            {"story_id": "intro", "idx": 0, "duration": 3, "subject": "Intro", "asset_type": "title_card", "preferred_source": "apple.com", "description": "Title card"},
            {"story_id": "s_01", "idx": 1, "duration": 12, "subject": "iPhone 18 Pro", "asset_type": "official_product_image", "preferred_source": "apple.com", "description": "iPhone close-up"},
        ]
    }
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "assets").mkdir()

    def fake_get(url, headers=None, timeout=None, stream=False):
        m = MagicMock()
        if "apple.com/iphone" in url and not url.endswith(".jpg"):
            m.status_code = 200
            m.text = SAMPLE_HTML_APPLE
            m.content = SAMPLE_HTML_APPLE.encode()
            m.headers = {"Content-Type": "text/html"}
            return m
        if url.endswith(".jpg"):
            m.status_code = 200
            m.headers = {"Content-Type": "image/jpeg"}
            # minimal jpeg: 1x1
            from io import BytesIO
            from PIL import Image
            im = Image.new("RGB", (1200, 630), color="red")
            buf = BytesIO()
            im.save(buf, format="JPEG")
            m.content = buf.getvalue()
            return m
        m.status_code = 404
        m.text = ""
        m.content = b""
        m.headers = {}
        return m

    with patch("verticals.asset_resolver.requests.get", side_effect=fake_get):
        assets = resolve_assets(shots, editorial, articles, run_dir)
        assert len(assets) == 2
        # intro synthetic
        assert assets[0]["status"] == "synthetic"
        assert assets[0]["story_id"] == "intro"
        # s_01 found
        assert assets[1]["status"] == "found"
        assert assets[1]["url"] == "https://www.apple.com/v/iphone/home/og.jpg"
        assert assets[1]["width"] == 1200

def test_resolve_missing(tmp_path):
    editorial = {
        "stories": [{"story_id": "s_01", "headline": "Test", "sources": ["rss_a1"]}]
    }
    articles = [
        {"article_id": "rss_a1", "title": "Test", "url": "https://www.macrumors.com/test", "source": "macrumors"},
    ]
    shots = {
        "edition": "2026-09-22",
        "shots": [
            {"story_id": "s_01", "idx": 0, "duration": 10, "subject": "Test product", "asset_type": "official_product_image", "preferred_source": "apple.com", "description": "Test image"},
        ]
    }
    run_dir = tmp_path / "run2"
    run_dir.mkdir()
    (run_dir / "assets").mkdir()

    def fake_get_fail(url, headers=None, timeout=None, stream=False):
        m = MagicMock()
        m.status_code = 404
        m.text = ""
        m.content = b""
        m.headers = {}
        return m

    with patch("verticals.asset_resolver.requests.get", side_effect=fake_get_fail):
        assets = resolve_assets(shots, editorial, articles, run_dir)
        assert assets[0]["status"] == "missing"
        assert assets[0]["url"] is None
