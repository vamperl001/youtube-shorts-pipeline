"""Tests for Phase 10 YouTube."""

from unittest.mock import patch, MagicMock
import json
from pathlib import Path

from verticals.youtube import _build_youtube_metadata

SAMPLE_EDITORIAL = {
    "stories": [
        {"story_id": "s_01", "headline": "iPhone 18 Pro camera leak reveals new sensor", "importance": 0.85, "status": "reported", "sources": ["rss_a1"]},
        {"story_id": "s_02", "headline": "watchOS 27.2 beta released", "importance": 0.7, "status": "reported", "sources": ["rss_a2"]},
    ]
}
SAMPLE_SCRIPT = {
    "intro": "Good morning",
    "stories": [{"story_id": "s_01", "text": "Apple leaked..."}, {"story_id": "s_02", "text": "watchOS..."}],
    "outro": "More tomorrow",
    "full_script": "Good morning Apple leaked... More tomorrow",
}

def test_build_metadata():
    meta = _build_youtube_metadata(SAMPLE_EDITORIAL, SAMPLE_SCRIPT)
    assert "youtube_title" in meta
    assert len(meta["youtube_title"]) <= 100
    assert "apple" in meta["youtube_tags"]
    assert "youtube_description" in meta

def test_upload_mock(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "editorial.json").write_text(json.dumps(SAMPLE_EDITORIAL))
    (run / "script.json").write_text(json.dumps(SAMPLE_SCRIPT))
    (run / "final.mp4").write_bytes(b"fake mp4")
    (run / "final.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello")
    # mock upload.upload_to_youtube (the real uploader)
    with patch("verticals.upload.upload_to_youtube", return_value="https://youtu.be/abc123") as mock:
        from verticals.youtube import upload_youtube
        meta = upload_youtube(run, lang="en")
        assert meta["url"] == "https://youtu.be/abc123"
        assert mock.called
