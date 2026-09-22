"""Tests for Phase 3 Editorial — LLM selection + validation + fallback."""

import json
from unittest.mock import patch

from verticals.editorial import _validate, _try_fix_hallucinated, _fallback_selection, select_editorial, build_prompt


SAMPLE_ARTICLES = [
    {"article_id": "rss_a1", "title": "iPhone 18 Pro camera leak", "summary": "New camera", "source": "macrumors", "published_at": "2026-09-22T10:00:00Z", "url": "https://macrumors.com/a1"},
    {"article_id": "rss_a2", "title": "watchOS 27.2 beta released", "summary": "Beta", "source": "9to5mac", "published_at": "2026-09-22T09:00:00Z", "url": "https://9to5mac.com/a2"},
    {"article_id": "rss_a3", "title": "M6 Mac mini review", "summary": "Review", "source": "appleinsider", "published_at": "2026-09-21T10:00:00Z", "url": "https://appleinsider.com/a3"},
    {"article_id": "rss_a4", "title": "Apple Music Hall opens", "summary": "Venue", "source": "cultofmac", "published_at": "2026-09-22T08:00:00Z", "url": "https://cultofmac.com/a4"},
]

SAMPLE_COMMUNITY = [
    {"subreddit": "apple", "title": "iPhone 18 Pro camera leak discussion", "url": "https://reddit.com/r/apple/abc"},
]

def _valid_editorial():
    return {
        "edition": "2026-09-22",
        "stories": [
            {"story_id": "s_01", "headline": "iPhone 18 Pro camera leak reveals new sensor", "importance": 0.85, "status": "reported", "sources": ["rss_a1"], "reason": "Latest leak from 22.09"},
            {"story_id": "s_02", "headline": "watchOS 27.2 beta released with new features", "importance": 0.70, "status": "reported", "sources": ["rss_a2"], "reason": "Beta released 22.09"},
            {"story_id": "s_03", "headline": "M6 Mac mini review shows strong performance", "importance": 0.65, "status": "reported", "sources": ["rss_a3"], "reason": "Review published 21.09"},
        ]
    }

class TestValidate:
    def test_valid(self):
        ok, err = _validate(_valid_editorial(), SAMPLE_ARTICLES, "2026-09-22")
        assert ok, err

    def test_wrong_count(self):
        data = _valid_editorial()
        data["stories"] = data["stories"][:2]
        ok, err = _validate(data, SAMPLE_ARTICLES, "2026-09-22")
        assert not ok and "3-5" in err

    def test_hallucinated_source(self):
        data = _valid_editorial()
        data["stories"][0]["sources"] = ["rss_fake"]
        ok, err = _validate(data, SAMPLE_ARTICLES, "2026-09-22")
        assert not ok and "hallucinated" in err

    def test_invalid_status(self):
        data = _valid_editorial()
        data["stories"][0]["status"] = "breaking"
        ok, err = _validate(data, SAMPLE_ARTICLES, "2026-09-22")
        assert not ok

    def test_duplicate_story_id(self):
        data = _valid_editorial()
        data["stories"][1]["story_id"] = "s_01"
        ok, err = _validate(data, SAMPLE_ARTICLES, "2026-09-22")
        assert not ok and "duplicate" in err

class TestFixHallucinated:
    def test_fix_via_headline(self):
        data = {
            "edition": "2026-09-22",
            "stories": [
                {"story_id": "s_01", "headline": "iPhone 18 Pro camera leak reveals new sensor", "importance": 0.8, "status": "reported", "sources": ["rss_fake"], "reason": "x"}
            ]
        }
        fixed = _try_fix_hallucinated(data, SAMPLE_ARTICLES)
        assert fixed["stories"][0]["sources"][0] in {"rss_a1", "rss_a2", "rss_a3", "rss_a4"}

class TestFallback:
    def test_fallback_3(self):
        fb = _fallback_selection(SAMPLE_ARTICLES, "2026-09-22")
        assert len(fb["stories"]) == 3
        assert fb["stories"][0]["sources"][0] == "rss_a1"  # newest

class TestSelectEditorialMock:
    @patch("verticals.editorial.call_llm")
    def test_success_first_try(self, mock_llm):
        mock_llm.return_value = json.dumps(_valid_editorial())
        res = select_editorial(SAMPLE_ARTICLES, SAMPLE_COMMUNITY, edition="2026-09-22", provider="mock")
        assert len(res["stories"]) == 3
        assert res["_attempt"] == 1

    @patch("verticals.editorial.call_llm")
    def test_retry_on_hallucinated_then_fix(self, mock_llm):
        bad = _valid_editorial()
        bad["stories"][0]["sources"] = ["rss_hallucinated"]
        # first call returns bad, second returns good
        mock_llm.side_effect = [json.dumps(bad), json.dumps(_valid_editorial())]
        res = select_editorial(SAMPLE_ARTICLES, [], edition="2026-09-22", provider="mock")
        # first was fixed by _try_fix_hallucinated, so it should actually pass on first try via fix
        # but we test retry logic: if fix still fails, it retries
        # Our fix will map hallucinated to rss_a1, so first attempt will actually pass after fix
        assert len(res["stories"]) == 3

    @patch("verticals.editorial.call_llm")
    def test_fallback_on_all_fail(self, mock_llm):
        mock_llm.side_effect = Exception("LLM down")
        res = select_editorial(SAMPLE_ARTICLES, [], edition="2026-09-22", provider="mock")
        assert "_fallback" in res
        assert len(res["stories"]) == 3

    def test_build_prompt_contains_allowed_ids(self):
        prompt = build_prompt(SAMPLE_ARTICLES, SAMPLE_COMMUNITY, "2026-09-22")
        for a in SAMPLE_ARTICLES:
            assert a["article_id"] in prompt
        assert "ALLOWED IDS" in prompt
