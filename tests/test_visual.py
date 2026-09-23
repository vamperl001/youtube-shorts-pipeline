"""Tests for Phase 5 Visual — shotlist without URLs."""

import json
from unittest.mock import patch

from verticals.visual import _validate_visual, _fallback_visual, generate_visual_plan, build_visual_prompt


SAMPLE_EDITORIAL = {
    "edition": "2026-09-22",
    "stories": [
        {"story_id": "s_01", "headline": "iPhone 18 Pro camera leak", "importance": 0.85, "status": "reported", "sources": ["rss_a1"]},
        {"story_id": "s_02", "headline": "watchOS 27.2 beta released", "importance": 0.70, "status": "reported", "sources": ["rss_a2"]},
        {"story_id": "s_03", "headline": "M6 Mac mini review", "importance": 0.65, "status": "reported", "sources": ["rss_a3"]},
    ]
}
SAMPLE_SCRIPT = {
    "edition": "2026-09-22",
    "intro": "Good morning, it's September 22 — three Apple headlines in 60 seconds.",
    "stories": [
        {"story_id": "s_01", "text": "Apple leaked the iPhone 18 Pro camera sensor. It promises better low light.", "duration_target": 12},
        {"story_id": "s_02", "text": "watchOS 27.2 beta is out with a new App Switcher gesture.", "duration_target": 10},
        {"story_id": "s_03", "text": "The M6 Mac mini review shows strong performance but high price.", "duration_target": 12},
    ],
    "outro": "More tomorrow.",
}

def _valid_visual():
    return {
        "edition": "2026-09-22",
        "shots": [
            {"story_id": "intro", "idx": 0, "duration": 3, "subject": "Apple News Intro", "asset_type": "title_card", "preferred_source": "apple.com", "description": "Minimal title card with Apple logo, white background, studio lighting"},
            {"story_id": "s_01", "idx": 1, "duration": 12, "subject": "iPhone 18 Pro camera", "asset_type": "official_product_image", "preferred_source": "apple.com", "description": "Close-up of iPhone 18 Pro camera module, studio lighting, 4K"},
            {"story_id": "s_02", "idx": 2, "duration": 10, "subject": "watchOS App Switcher", "asset_type": "official_screenshot", "preferred_source": "apple.com", "description": "Screenshot of watchOS App Switcher gesture, clean UI, 4K"},
            {"story_id": "s_03", "idx": 3, "duration": 12, "subject": "Mac mini", "asset_type": "official_product_image", "preferred_source": "apple.com", "description": "M6 Mac mini on desk, dark room, LED, 4K"},
            {"story_id": "outro", "idx": 4, "duration": 3, "subject": "Apple News Outro", "asset_type": "title_card", "preferred_source": "apple.com", "description": "Outro title card with 'More tomorrow', minimal, 4K"},
        ]
    }

class TestValidateVisual:
    def test_valid(self):
        ok, err = _validate_visual(_valid_visual(), SAMPLE_EDITORIAL, SAMPLE_SCRIPT)
        assert ok, err

    def test_wrong_count(self):
        data = _valid_visual()
        data["shots"] = data["shots"][:2]
        ok, err = _validate_visual(data, SAMPLE_EDITORIAL, SAMPLE_SCRIPT)
        assert not ok and "3-7" in err

    def test_invalid_asset_type(self):
        data = _valid_visual()
        data["shots"][1]["asset_type"] = "stock_image"
        ok, err = _validate_visual(data, SAMPLE_EDITORIAL, SAMPLE_SCRIPT)
        assert not ok

    def test_invalid_source(self):
        data = _valid_visual()
        data["shots"][1]["preferred_source"] = "pexels.com"
        ok, err = _validate_visual(data, SAMPLE_EDITORIAL, SAMPLE_SCRIPT)
        assert not ok

    def test_url_in_description(self):
        data = _valid_visual()
        data["shots"][1]["description"] = "Close-up of iPhone with link https://apple.com/image.jpg for reference and more details here"
        ok, err = _validate_visual(data, SAMPLE_EDITORIAL, SAMPLE_SCRIPT)
        assert not ok and "URL" in err

    def test_unknown_story_id(self):
        data = _valid_visual()
        data["shots"][1]["story_id"] = "s_99"
        ok, err = _validate_visual(data, SAMPLE_EDITORIAL, SAMPLE_SCRIPT)
        assert not ok

class TestFallback:
    def test_fallback(self):
        fb = _fallback_visual(SAMPLE_EDITORIAL, SAMPLE_SCRIPT, "2026-09-22")
        assert len(fb["shots"]) == 5  # intro + 3 + outro
        assert fb["shots"][0]["story_id"] == "intro"
        assert fb["shots"][-1]["story_id"] == "outro"

class TestBuildPrompt:
    def test_contains_story_ids(self):
        prompt = build_visual_prompt(SAMPLE_SCRIPT, SAMPLE_EDITORIAL, "2026-09-22", niche="apple")
        for s in SAMPLE_EDITORIAL["stories"]:
            assert s["story_id"] in prompt

class TestGenerateVisualMock:
    @patch("verticals.visual.call_llm")
    def test_success(self, mock_llm):
        mock_llm.return_value = json.dumps(_valid_visual())
        res = generate_visual_plan(SAMPLE_SCRIPT, SAMPLE_EDITORIAL, edition="2026-09-22", niche="apple", provider="mock")
        assert len(res["shots"]) == 5
        assert res["_attempt"] == 1

    @patch("verticals.visual.call_llm")
    def test_fallback(self, mock_llm):
        mock_llm.side_effect = Exception("LLM down")
        res = generate_visual_plan(SAMPLE_SCRIPT, SAMPLE_EDITORIAL, edition="2026-09-22", niche="apple", provider="mock")
        assert "_fallback" in res
