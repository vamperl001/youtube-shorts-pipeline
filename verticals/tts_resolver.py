"""TTS Resolver — Phase 7: script full_script -> voiceover.mp3 (Edge frei, kein Hardcode).

Nutzt bestehendes verticals/tts.py (Edge 4x Retry -> Fallback), speichert in runs/<ts>.
"""

import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .log import log

def resolve_tts(script_data: dict, run_dir: Path, niche: str = "apple", lang: str = "en", provider: str | None = None) -> dict:
    """Generiere Voiceover aus script full_script, speichere in run_dir."""
    from .tts import generate_voiceover
    from .niche import load_niche, get_voice_config
    from .assemble import get_audio_duration  # reuse ffprobe helper

    run_dir = Path(run_dir)
    full_script = script_data.get("full_script") or (script_data.get("intro","") + " " + " ".join(s.get("text","") for s in script_data.get("stories",[])) + " " + script_data.get("outro",""))
    full_script = full_script.strip()
    if not full_script:
        raise ValueError("Kein full_script in script.json")

    # voice config aus niche
    try:
        profile = load_niche(niche or "apple")
        voice_config = get_voice_config(profile, provider=provider or "edge", lang=lang)
    except Exception:
        voice_config = {}

    # actual voice for log (edge pool is random)
    actual_voice = voice_config.get("voice_id", "")
    if not actual_voice and (provider or "edge") == "edge":
        try:
            from .tts import _pick_edge_voice
            actual_voice = _pick_edge_voice(lang, "")
            # edge pool random, but we log the pick attempt (actual may differ due to fallback, ponytail: log pool)
        except Exception:
            actual_voice = "edge-pool"

    start = time.monotonic()
    voice_path = generate_voiceover(full_script, run_dir, lang=lang, provider=provider, voice_config=voice_config)
    duration = 0.0
    try:
        duration = get_audio_duration(voice_path)
    except Exception as e:
        log(f"TTS duration probe fehlgeschlagen: {e}")

    # hash for observability
    try:
        h = hashlib.sha256(voice_path.read_bytes()).hexdigest()[:12]
    except Exception:
        h = None

    elapsed = time.monotonic() - start
    # also copy to canonical name voiceover.mp3 for next phases
    canonical = run_dir / "voiceover.mp3"
    if voice_path != canonical and voice_path.exists():
        try:
            import shutil
            shutil.copy2(voice_path, canonical)
        except Exception:
            pass
        voice_path = canonical

    return {
        "provider": provider or "edge",
        "voice_id": actual_voice or voice_config.get("voice_id", "edge"),
        "lang": lang,
        "text_length": len(full_script),
        "word_count": len(full_script.split()),
        "duration": round(duration, 2),
        "file_path": str(voice_path.relative_to(run_dir)) if voice_path.is_relative_to(run_dir) else str(voice_path),
        "hash": h,
        "elapsed_ms": int(elapsed * 1000),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

def save_tts(run_dir: Path, tts_meta: dict):
    import json
    run_dir = Path(run_dir)
    out = run_dir / "tts.json"
    out.write_text(json.dumps(tts_meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Saved tts → {out} ({tts_meta.get('duration')}s)")
