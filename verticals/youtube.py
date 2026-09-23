"""YouTube Upload Phase 10 — final.mp4 + editorial/script metadata -> private upload.

Nutzt bestehendes verticals/upload.py (google-api, resumable), speichert youtube.json.
"""

import json
from pathlib import Path
from datetime import datetime, timezone

from .log import log


def _build_youtube_metadata(editorial: dict, script: dict) -> dict:
    """Bilde Titel/Beschreibung/Tags aus editorial+script (kein LLM)."""
    # Titel: erste Story headline oder editorial top
    stories = editorial.get("stories", [])
    if stories:
        title = stories[0].get("headline", "")[:95]
        # YouTube title max 100
        if len(title) < 5:
            title = script.get("intro", "")[:95]
    else:
        title = script.get("intro", "")[:95] or "Apple News Daily"
    # Beschreibung: full_script + Quellen
    desc_parts = []
    full = script.get("full_script", "")
    if full:
        desc_parts.append(full[:4000])
    # Quellen
    articles = editorial.get("stories", [])
    # Tags: from headlines + apple
    tags = set()
    for s in stories:
        for w in s.get("headline", "").lower().split():
            w = w.strip(".,!?:;()[]{}\"'")
            if len(w) >= 4 and w not in {"apple", "with", "from", "this", "that"}:
                tags.add(w)
    tags.update(["apple", "iphone", "ios", "mac", "apple news"])
    tags_str = ",".join(list(tags)[:15])
    desc = "\n\n".join(desc_parts) if desc_parts else title
    if len(desc) > 4500:
        desc = desc[:4500]
    return {
        "youtube_title": title[:100],
        "youtube_description": desc,
        "youtube_tags": tags_str,
    }

def upload_youtube(run_dir: Path, lang: str = "en", privacy: str = "private") -> dict:
    """Upload final.mp4 aus run_dir, return youtube meta."""
    run_dir = Path(run_dir)
    final = run_dir / "final.mp4"
    if not final.exists():
        raise FileNotFoundError(f"final.mp4 fehlt in {run_dir}")

    editorial = json.loads((run_dir / "editorial.json").read_text()) if (run_dir / "editorial.json").exists() else {}
    script = json.loads((run_dir / "script.json").read_text()) if (run_dir / "script.json").exists() else {}
    # editorial_clean for metadata
    editorial_clean = {k: v for k, v in editorial.items() if not k.startswith("_")}
    script_clean = {k: v for k, v in script.items() if not k.startswith("_")}

    meta = _build_youtube_metadata(editorial_clean, script_clean)
    # build draft-like dict for upload.py
    draft = {
        "news": meta["youtube_title"],
        "youtube_title": meta["youtube_title"],
        "youtube_description": meta["youtube_description"],
        "youtube_tags": meta["youtube_tags"],
    }

    # srt for captions
    srt_path = run_dir / "final.srt"
    if not srt_path.exists():
        srt_path = run_dir / "tts.json"  # dummy, upload will skip if not exists
        srt_path = None
        # try to find any srt
        for cand in [run_dir / "final.srt", run_dir / "captions.srt", run_dir / "voiceover.srt"]:
            if cand.exists():
                srt_path = cand
                break
        else:
            srt_path = None
            # try run_dir / "final.srt" already
            if (run_dir / "final.srt").exists():
                srt_path = run_dir / "final.srt"

    # thumbnail: try assets or generate? ponytail: use first asset if found
    thumb_path = None
    assets_path = run_dir / "assets.json"
    if assets_path.exists():
        try:
            assets = json.loads(assets_path.read_text())
            for a in assets:
                if a.get("status") == "found" and a.get("file_path"):
                    cand = run_dir / a["file_path"]
                    if cand.exists():
                        # use as thumbnail source? need 1280x720 thumb, we can use first found asset as thumb
                        # For now, try to use it directly if it's jpg
                        thumb_path = cand
                        break
        except Exception:
            pass
    # if no thumb, upload without

    from .upload import upload_to_youtube
    # upload_to_youtube expects video_path, draft, srt_path, lang, thumb_path
    # it will handle privacy private by default
    url = upload_to_youtube(final, draft, srt_path, lang, thumb_path)
    return {
        "url": url,
        "video_path": str(final),
        "srt_path": str(srt_path) if srt_path else None,
        "thumb_path": str(thumb_path) if thumb_path else None,
        "title": meta["youtube_title"],
        "tags": meta["youtube_tags"],
        "privacy": privacy,
        "uploaded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

def save_youtube(run_dir: Path, yt_meta: dict):
    import json
    run_dir = Path(run_dir)
    out = run_dir / "youtube.json"
    out.write_text(json.dumps(yt_meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Saved youtube → {out} ({yt_meta.get('url')})")
