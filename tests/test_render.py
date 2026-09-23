"""Tests for Phase 8 Render — mocked ffmpeg/whisper."""

from pathlib import Path
from unittest.mock import patch, MagicMock
import json

from verticals.render import _generate_title_card


def test_title_card(tmp_path):
    out = tmp_path / "card.jpg"
    _generate_title_card("Test Title Apple News", out)
    assert out.exists()
    assert out.stat().st_size > 5_000

def test_render_mock(tmp_path):
    # create minimal run dir
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # shots
    shots = {
        "edition": "2026-09-22",
        "shots": [
            {"story_id": "s_01", "idx": 0, "duration": 5, "subject": "iPhone", "asset_type": "official_product_image", "preferred_source": "apple.com", "description": "iPhone"},
            {"story_id": "s_02", "idx": 1, "duration": 5, "subject": "Watch", "asset_type": "official_product_image", "preferred_source": "apple.com", "description": "Watch"},
        ]
    }
    (run_dir / "shots.json").write_text(json.dumps(shots))
    (run_dir / "editorial.json").write_text(json.dumps({"stories": [{"story_id": "s_01"}, {"story_id": "s_02"}]}))
    (run_dir / "assets.json").write_text(json.dumps([
        {"shot_idx": 0, "status": "found", "file_path": "assets/shot_00.jpg"},
        {"shot_idx": 1, "status": "found", "file_path": "assets/shot_01.jpg"},
    ]))
    assets_dir = run_dir / "assets"
    assets_dir.mkdir()
    # create dummy assets
    from PIL import Image
    for i in range(2):
        im = Image.new("RGB", (1200, 800), color=(i*100, 100, 100))
        im.save(assets_dir / f"shot_{i:02d}.jpg")
    # voiceover dummy
    voice = run_dir / "voiceover.mp3"
    voice.write_bytes(b"fake mp3 " * 1000)

    # mock dependencies
    with patch("verticals.render.get_audio_duration", return_value=10.0):
        with patch("verticals.render.animate_frame") as mock_anim:
            def fake_anim(frame, out, dur, effect):
                Path(out).write_bytes(b"fake mp4")
            mock_anim.side_effect = fake_anim
            with patch("verticals.render.run_cmd") as mock_run:
                mock_run.return_value = MagicMock()
                # mock whisper
                with patch("verticals.captions._whisper_word_timestamps", return_value=[]):
                    from verticals.render import render_video
                    meta = render_video(run_dir, lang="en")
                    assert meta["shots"] == 2
                    assert "final_path" in meta
                    assert meta["duration"] == 10.0
