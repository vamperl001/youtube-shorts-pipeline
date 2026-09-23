"""Tests for Phase 9 QC."""

import json
from pathlib import Path

from verticals.qc import _check_editorial, _check_script, _check_assets, _check_video, run_qc


SAMPLE_ARTICLES = [{"article_id": "rss_a1"}, {"article_id": "rss_a2"}]
SAMPLE_EDITORIAL = {
    "edition": "2026-09-22",
    "stories": [
        {"story_id": "s_01", "headline": "iPhone leak", "importance": 0.8, "status": "reported", "sources": ["rss_a1"], "reason": "x"},
        {"story_id": "s_02", "headline": "watchOS beta", "importance": 0.7, "status": "reported", "sources": ["rss_a2"], "reason": "x"},
        {"story_id": "s_03", "headline": "Mac mini", "importance": 0.6, "status": "reported", "sources": ["rss_a1"], "reason": "x"},
    ]
}
SAMPLE_SCRIPT = {
    "intro": "Good morning, it's September 22 — three Apple headlines in 60 seconds from the Apple ecosystem and beyond.",
    "outro": "More tomorrow. Save this before your next update and follow for daily Apple breakdowns.",
    "stories": [
        {"story_id": "s_01", "text": "Apple leaked the iPhone 18 Pro camera sensor with a new Texture and Grain control that promises better low light performance and will be limited to the latest Pro models.", "duration_target": 12},
        {"story_id": "s_02", "text": "watchOS 27.2 beta is out with a new App Switcher gesture that brings back the classic double-click and users praised the return after the redesign removed it.", "duration_target": 12},
        {"story_id": "s_03", "text": "The M6 Mac mini review shows strong performance but high price and reviewers note the compact design is great yet the value proposition has weakened compared to last year.", "duration_target": 14},
    ],
    "word_count": 110,
}
SAMPLE_SHOTS = [
    {"idx": 0, "story_id": "intro", "duration": 3, "subject": "Intro", "asset_type": "title_card", "preferred_source": "apple.com", "description": "Title card"},
    {"idx": 1, "story_id": "s_01", "duration": 12, "subject": "iPhone", "asset_type": "official_product_image", "preferred_source": "apple.com", "description": "iPhone close-up"},
]
SAMPLE_ASSETS = [
    {"shot_idx": 0, "status": "synthetic", "source_domain": "synthetic"},
    {"shot_idx": 1, "status": "found", "source_domain": "apple.com", "width": 1200, "height": 630, "ratio": 1.9},
]

def test_editorial_pass():
    res = _check_editorial(SAMPLE_EDITORIAL, SAMPLE_ARTICLES)
    assert res["pass"]

def test_editorial_fail():
    bad = {"edition": "2026-09-22", "stories": [{"story_id": "s_01", "headline": "x", "importance": 0.8, "status": "reported", "sources": ["rss_fake"], "reason": "x"}]}
    res = _check_editorial(bad, SAMPLE_ARTICLES)
    assert not res["pass"]

def test_script_pass():
    res = _check_script(SAMPLE_SCRIPT, SAMPLE_EDITORIAL)
    assert res["pass"]

def test_script_fail():
    bad = {"intro": "hi", "outro": "bye", "stories": [{"story_id": "s_01", "text": "x", "duration_target": 12}]}
    res = _check_script(bad, SAMPLE_EDITORIAL)
    assert not res["pass"]

def test_assets_pass():
    res = _check_assets(SAMPLE_ASSETS, SAMPLE_SHOTS)
    assert res["pass"]

def test_assets_missing():
    bad = [{"shot_idx": 0, "status": "missing", "source_domain": "apple.com"}]
    res = _check_assets(bad, SAMPLE_SHOTS)
    assert not res["pass"]

def test_run_qc(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "articles.json").write_text(json.dumps(SAMPLE_ARTICLES))
    (run / "editorial.json").write_text(json.dumps(SAMPLE_EDITORIAL))
    (run / "script.json").write_text(json.dumps(SAMPLE_SCRIPT))
    (run / "shots.json").write_text(json.dumps({"shots": SAMPLE_SHOTS}))
    (run / "assets.json").write_text(json.dumps(SAMPLE_ASSETS))
    # no final.mp4 -> video pending
    qc = run_qc(run)
    assert qc["checks"]["editorial"]["pass"]
    assert qc["checks"]["script"]["pass"]
    assert qc["checks"]["assets"]["pass"]
    assert qc["checks"]["video"]["pending"]
    assert qc["pass"]  # pending counts as pass
