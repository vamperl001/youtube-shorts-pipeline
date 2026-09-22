"""Tests for Phase 4 Script — spoken briefing."""

import json
from unittest.mock import patch

from verticals.script import _validate_script, _fallback_script, generate_script, build_script_prompt


SAMPLE_EDITORIAL = {
    "edition": "2026-09-22",
    "stories": [
        {"story_id": "s_01", "headline": "iPhone 18 Pro camera leak", "importance": 0.85, "status": "reported", "sources": ["rss_a1"], "reason": "Leak"},
        {"story_id": "s_02", "headline": "watchOS 27.2 beta released", "importance": 0.70, "status": "reported", "sources": ["rss_a2"], "reason": "Beta"},
        {"story_id": "s_03", "headline": "M6 Mac mini review", "importance": 0.65, "status": "reported", "sources": ["rss_a3"], "reason": "Review"},
    ]
}
SAMPLE_ARTICLES = [
    {"article_id": "rss_a1", "title": "iPhone 18 Pro camera leak", "summary": "New sensor", "source": "macrumors", "published_at": "2026-09-22T10:00:00Z"},
    {"article_id": "rss_a2", "title": "watchOS 27.2 beta released", "summary": "Beta features", "source": "9to5mac", "published_at": "2026-09-22T09:00:00Z"},
    {"article_id": "rss_a3", "title": "M6 Mac mini review", "summary": "Review", "source": "appleinsider", "published_at": "2026-09-21T10:00:00Z"},
]

def _valid_script():
    return {
        "edition": "2026-09-22",
        "intro": "Good morning, it's September 22 — three Apple headlines in 60 seconds from the Apple ecosystem.",
        "stories": [
            {"story_id": "s_01", "text": "Apple leaked the iPhone 18 Pro camera sensor with a new Texture and Grain control. It promises better low light performance and will be limited to the latest Pro models.", "duration_target": 12},
            {"story_id": "s_02", "text": "watchOS 27.2 beta is out with a new App Switcher gesture that brings back the classic double-click. Users praised the return after the last redesign removed it.", "duration_target": 12},
            {"story_id": "s_03", "text": "The M6 Mac mini review shows strong performance but high price. Reviewers note the compact design is great, yet the value proposition has weakened compared to last year.", "duration_target": 14},
        ],
        "outro": "More tomorrow. Save this before your next update and follow for daily Apple breakdowns.",
    }

class TestValidateScript:
    def test_valid(self):
        ok, err = _validate_script(_valid_script(), SAMPLE_EDITORIAL)
        assert ok, err

    def test_wrong_story_count(self):
        data = _valid_script()
        data["stories"] = data["stories"][:2]
        ok, err = _validate_script(data, SAMPLE_EDITORIAL)
        assert not ok and "count" in err

    def test_unknown_story_id(self):
        data = _valid_script()
        data["stories"][0]["story_id"] = "s_99"
        ok, err = _validate_script(data, SAMPLE_EDITORIAL)
        assert not ok and "not in editorial" in err

    def test_short_text(self):
        data = _valid_script()
        data["stories"][0]["text"] = "Hi"
        ok, err = _validate_script(data, SAMPLE_EDITORIAL)
        assert not ok

    def test_word_count(self):
        data = _valid_script()
        data["intro"] = "Hi"
        data["outro"] = "Bye"
        data["stories"] = [{"story_id": "s_01", "text": "short", "duration_target": 10}, {"story_id": "s_02", "text": "short", "duration_target": 10}, {"story_id": "s_03", "text": "short", "duration_target": 10}]
        ok, err = _validate_script(data, SAMPLE_EDITORIAL)
        assert not ok

class TestFallback:
    def test_fallback(self):
        fb = _fallback_script(SAMPLE_EDITORIAL, "2026-09-22")
        assert len(fb["stories"]) == 3
        assert fb["intro"].startswith("Good morning")
        assert fb["word_count"] >= 20

class TestBuildPrompt:
    def test_contains_ids(self):
        prompt = build_script_prompt(SAMPLE_EDITORIAL, SAMPLE_ARTICLES, "2026-09-22", niche="apple")
        for s in SAMPLE_EDITORIAL["stories"]:
            assert s["story_id"] in prompt
            assert s["headline"][:20] in prompt

class TestGenerateScriptMock:
    @patch("verticals.script.call_llm")
    def test_success(self, mock_llm):
        mock_llm.return_value = json.dumps(_valid_script())
        res = generate_script(SAMPLE_EDITORIAL, SAMPLE_ARTICLES, edition="2026-09-22", niche="apple", provider="mock")
        assert len(res["stories"]) == 3
        assert res["word_count"] > 20
        assert res["_attempt"] == 1

    @patch("verticals.script.call_llm")
    def test_fallback_on_failure(self, mock_llm):
        mock_llm.side_effect = Exception("LLM down")
        res = generate_script(SAMPLE_EDITORIAL, SAMPLE_ARTICLES, edition="2026-09-22", niche="apple", provider="mock")
        assert "_fallback" in res

    @patch("verticals.script.call_llm")
    def test_retry_on_invalid(self, mock_llm):
        bad = _valid_script()
        bad["stories"] = bad["stories"][:1]  # wrong count
        mock_llm.side_effect = [json.dumps(bad), json.dumps(_valid_script())]
        res = generate_script(SAMPLE_EDITORIAL, SAMPLE_ARTICLES, edition="2026-09-22", niche="apple", provider="mock")
        assert len(res["stories"]) == 3
