"""Tests for Phase 7 TTS Resolver."""

from pathlib import Path
from unittest.mock import patch, MagicMock
import json

from verticals.tts_resolver import resolve_tts, save_tts


SAMPLE_SCRIPT = {
    "edition": "2026-09-22",
    "intro": "Good morning, it's September 22 — three Apple headlines in 60 seconds.",
    "stories": [
        {"story_id": "s_01", "text": "Apple leaked the iPhone 18 Pro camera sensor.", "duration_target": 12},
        {"story_id": "s_02", "text": "watchOS 27.2 beta is out.", "duration_target": 10},
    ],
    "outro": "More tomorrow.",
    "full_script": "Good morning, it's September 22 — three Apple headlines in 60 seconds. Apple leaked the iPhone 18 Pro camera sensor. watchOS 27.2 beta is out. More tomorrow.",
    "word_count": 22,
}

def test_resolve_tts_mock(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # mock generate_voiceover to create dummy mp3
    def fake_generate(text, out_dir, lang="en", provider=None, voice_config=None):
        p = Path(out_dir) / f"voiceover_{lang}.mp3"
        p.write_bytes(b"fake mp3 content " * 1000)
        return p

    with patch("verticals.tts.generate_voiceover", side_effect=fake_generate):
        with patch("verticals.assemble.get_audio_duration", return_value=12.5):
            meta = resolve_tts(SAMPLE_SCRIPT, run_dir, niche="apple", lang="en", provider="edge")
            assert meta["word_count"] >= 20
            assert meta["duration"] == 12.5
            assert meta["provider"] == "edge"
            assert (run_dir / "voiceover.mp3").exists()
            assert meta["file_path"] == "voiceover.mp3"

def test_save_tts(tmp_path):
    run_dir = tmp_path / "run2"
    run_dir.mkdir()
    meta = {"provider": "edge", "duration": 10.2, "word_count": 20}
    save_tts(run_dir, meta)
    j = json.loads((run_dir / "tts.json").read_text())
    assert j["duration"] == 10.2

def test_missing_full_script(tmp_path):
    run_dir = tmp_path / "run3"
    run_dir.mkdir()
    bad = {"intro": "", "stories": [], "outro": ""}
    try:
        resolve_tts(bad, run_dir)
        assert False, "should raise"
    except ValueError:
        pass
