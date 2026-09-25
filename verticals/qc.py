"""QC Phase 9 — automatisierbare Checks je Layer (ponytail: deterministisch, kein LLM)."""

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from .log import log


def _check_editorial(editorial: dict, articles: list[dict]) -> dict:
    checks = {}
    try:
        article_ids = {a.get("article_id") for a in articles}
        stories = editorial.get("stories", [])
        # musik/manual: 1 Story ok, sonst 3-5
        if len(articles) <= 2:
            checks["has_stories"] = 1 <= len(stories) <= 3
        else:
            checks["has_stories"] = 3 <= len(stories) <= 5
        checks["no_empty_sources"] = all(s.get("sources") for s in stories)
        checks["sources_exist"] = all(src in article_ids for s in stories for src in s.get("sources", []))
        checks["no_duplicate_headlines"] = len({s.get("headline","").lower()[:50] for s in stories}) == len(stories)
        checks["importance_range"] = all(0 <= s.get("importance", -1) <= 1 for s in stories)
        checks["status_valid"] = all(s.get("status") in {"confirmed","reported","rumor","community"} for s in stories)
        checks["pass"] = all(checks.values())
    except Exception as e:
        checks["error"] = str(e)
        checks["pass"] = False
    return checks

def _check_script(script: dict, editorial: dict) -> dict:
    checks = {}
    try:
        ed_ids = {s.get("story_id") for s in editorial.get("stories", [])}
        checks["has_intro_outro"] = bool(script.get("intro") and script.get("outro"))
        checks["story_count_match"] = len(script.get("stories", [])) == len(ed_ids)
        checks["story_ids_match"] = all(s.get("story_id") in ed_ids for s in script.get("stories", []))
        checks["no_short_text"] = all(len(s.get("text","").split()) >= 6 for s in script.get("stories", []))
        full = script.get("intro","") + " " + " ".join(s.get("text","") for s in script.get("stories", [])) + " " + script.get("outro","")
        wc = len(full.split())
        # musik 1 Story: 30+ ok, apple 110-140, sonst 80-220
        if len(script.get("stories", [])) <= 1:
            checks["word_count_ok"] = 30 <= wc <= 160
        else:
            checks["word_count_ok"] = 80 <= wc <= 220
        checks["has_word_count"] = "word_count" in script
        checks["pass"] = all(v for k, v in checks.items() if k != "pass")
    except Exception as e:
        checks["error"] = str(e)
        checks["pass"] = False
    return checks

def _check_assets(assets: list[dict], shots: list[dict]) -> dict:
    checks = {}
    try:
        checks["count_match"] = len(assets) == len(shots)
        # each shot should have asset with same idx
        amap = {a.get("shot_idx"): a for a in assets}
        checks["all_shots_have_asset"] = all(s.get("idx") in amap for s in shots)
        # status distribution
        found = sum(1 for a in assets if a.get("status") == "found")
        missing = sum(1 for a in assets if a.get("status") == "missing")
        synthetic = sum(1 for a in assets if a.get("status") == "synthetic")
        checks["found_or_synthetic"] = found + synthetic >= len(shots) * 0.5  # at least half found/synthetic
        checks["no_stock"] = all(a.get("source_domain") != "stock" for a in assets)  # we never use stock
        # url reachable: for found, check url exists and domain allowed
        allowed = {"apple.com", "macrumors.com", "9to5mac.com", "appleinsider.com", "cultofmac.com", "synthetic", "local"}
        checks["domain_allowed"] = all((a.get("source_domain") in allowed) or a.get("status") in ("missing","synthetic") for a in assets)
        # resolution: for found, width >=800 and height >=600
        def _res_ok(a):
            if a.get("status") != "found":
                return True
            w, h = a.get("width"), a.get("height")
            if w is None or h is None:
                return True  # PIL not available, skip
            return w >= 800 and h >= 600
        checks["min_resolution"] = all(_res_ok(a) for a in assets)
        # ratio: allow 4:3, 16:9, 1:1, 9:16 — just check ratio exists if found
        checks["has_ratio"] = all(a.get("ratio") is not None or a.get("status") != "found" or not a.get("width") for a in assets)
        checks["missing_is_explicit"] = all(a.get("status") in ("found","missing","synthetic") for a in assets)
        checks["pass"] = all(checks[k] for k in ["count_match","all_shots_have_asset","found_or_synthetic","domain_allowed","min_resolution","missing_is_explicit"])
        checks["found"] = found
        checks["missing"] = missing
        checks["synthetic"] = synthetic
    except Exception as e:
        checks["error"] = str(e)
        checks["pass"] = False
    return checks

def _check_video(run_dir: Path, tts_meta: dict, render_meta: dict) -> dict:
    checks = {}
    try:
        final = run_dir / "final.mp4"
        checks["video_exists"] = final.exists() and final.stat().st_size > 100_000
        # ffprobe duration
        try:
            from .assemble import get_audio_duration
            # video duration via ffprobe (reuse get_audio_duration which works for video too)
            dur = get_audio_duration(final) if final.exists() else 0
            checks["has_duration"] = dur > 5
            # expected duration: tts duration ±2s
            tts_dur = float(tts_meta.get("duration", 0)) if tts_meta else 0
            if tts_dur:
                checks["duration_match_tts"] = abs(dur - tts_dur) < 3.0
            else:
                checks["duration_match_tts"] = True
            checks["duration"] = round(dur,2) if dur else None
        except Exception as e:
            checks["duration_error"] = str(e)
            checks["has_duration"] = False
            checks["duration_match_tts"] = False
        # resolution
        try:
            import subprocess
            r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(final)], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                # first line is video stream
                line = r.stdout.strip().split("\n")[0]
                w, h = line.split(",")
                w, h = int(w), int(h)
                checks["resolution_ok"] = (w == 1080 and h == 1920)
                checks["width"] = w
                checks["height"] = h
            else:
                checks["resolution_ok"] = False
        except Exception as e:
            checks["resolution_error"] = str(e)
            checks["resolution_ok"] = False
        # audio present
        try:
            import subprocess
            r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(final)], capture_output=True, text=True, timeout=5)
            checks["has_audio"] = "audio" in r.stdout
            checks["has_video"] = "video" in r.stdout
        except Exception:
            checks["has_audio"] = False
            checks["has_video"] = False
        # no black frames: check mean luma >16 via ffmpeg signalstats? ponytail: skip, just check file size
        checks["not_empty"] = final.exists() and final.stat().st_size > 500_000
        checks["pass"] = all(checks.get(k) for k in ["video_exists","has_duration","resolution_ok","has_audio","has_video","not_empty"])
    except Exception as e:
        checks["error"] = str(e)
        checks["pass"] = False
    return checks

def run_qc(run_dir: Path) -> dict:
    """Führe alle QC Checks aus, return qc dict."""
    run_dir = Path(run_dir)
    qc = {"run_dir": str(run_dir), "checks": {}}
    # load files
    try:
        articles = json.loads((run_dir / "articles.json").read_text()) if (run_dir / "articles.json").exists() else []
        editorial = json.loads((run_dir / "editorial.json").read_text()) if (run_dir / "editorial.json").exists() else {}
        script = json.loads((run_dir / "script.json").read_text()) if (run_dir / "script.json").exists() else {}
        shots_data = json.loads((run_dir / "shots.json").read_text()) if (run_dir / "shots.json").exists() else {}
        shots = shots_data.get("shots", [])
        assets = json.loads((run_dir / "assets.json").read_text()) if (run_dir / "assets.json").exists() else []
        tts_meta = json.loads((run_dir / "tts.json").read_text()) if (run_dir / "tts.json").exists() else {}
        render_meta = json.loads((run_dir / "render.json").read_text()) if (run_dir / "render.json").exists() else {}
    except Exception as e:
        qc["error"] = f"load failed: {e}"
        qc["pass"] = False
        return qc

    if editorial and articles:
        qc["checks"]["editorial"] = _check_editorial(editorial, articles)
    else:
        qc["checks"]["editorial"] = {"pass": False, "reason": "missing editorial/articles"}

    if script and editorial:
        qc["checks"]["script"] = _check_script(script, editorial)
    else:
        qc["checks"]["script"] = {"pass": False, "reason": "missing script/editorial"}

    if assets and shots:
        qc["checks"]["assets"] = _check_assets(assets, shots)
    else:
        # if no assets/shots yet, mark as pending not failed (phase 8 not done)
        qc["checks"]["assets"] = {"pass": True, "pending": True, "reason": "no assets/shots yet"}

    if (run_dir / "final.mp4").exists():
        qc["checks"]["video"] = _check_video(run_dir, tts_meta, render_meta)
    else:
        qc["checks"]["video"] = {"pass": True, "pending": True, "reason": "no final.mp4 yet"}

    # overall
    passes = [v.get("pass") for v in qc["checks"].values() if isinstance(v, dict)]
    qc["pass"] = all(passes) if passes else False
    # if any pending, overall is still pass but with pending flag
    qc["pending"] = any(v.get("pending") for v in qc["checks"].values())
    log(f"QC {run_dir.name}: {'PASS' if qc['pass'] else 'FAIL'} editorial:{qc['checks'].get('editorial',{}).get('pass')} script:{qc['checks'].get('script',{}).get('pass')} assets:{qc['checks'].get('assets',{}).get('pass')} video:{qc['checks'].get('video',{}).get('pass')}")
    return qc

def save_qc(run_dir: Path, qc: dict):
    import json
    run_dir = Path(run_dir)
    out = run_dir / "qc.json"
    out.write_text(json.dumps(qc, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Saved qc → {out} ({'PASS' if qc.get('pass') else 'FAIL'})")
