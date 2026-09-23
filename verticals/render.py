"""Render Phase 8 — Assets + Voiceover (+ Captions) -> final.mp4 (9:16).

Nutzt broll.animate_frame / prepare_source_frame, captions Whisper, ffmpeg xfade.
Run-basiert: runs/<ts>/shots.json + assets.json + voiceover.mp3 + script.json -> runs/<ts>/final.mp4
"""

import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .log import log
from .config import run_cmd
from .broll import animate_frame, prepare_source_frame
from .assemble import get_audio_duration, _ffmpeg_has_libass


def _generate_title_card(text: str, out_path: Path, size=(1080, 1920)):
    """Synthetic title card for intro/outro/missing."""
    w, h = size
    # dark background
    img = Image.new("RGB", (w, h), (15, 15, 30))
    draw = ImageDraw.Draw(img)
    # try DejaVu
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    except Exception:
        font = ImageFont.load_default()
        small = font
    # wrap text
    words = text.split()
    lines = []
    cur = ""
    for wrd in words:
        test = cur + " " + wrd if cur else wrd
        # estimate width
        try:
            bbox = draw.textbbox((0, 0), test, font=font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(test) * 20
        if tw > w - 120:
            lines.append(cur)
            cur = wrd
        else:
            cur = test
    if cur:
        lines.append(cur)
    # draw centered
    y = h // 2 - len(lines) * 30
    for line in lines:
        try:
            bbox = draw.textbbox((0, 0), line, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = len(line) * 18, 30
        x = (w - tw) // 2
        # shadow
        draw.text((x+3, y+3), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += th + 12
    # small footer
    footer = "Apple News  •  gregsplace"
    try:
        bbox = draw.textbbox((0, 0), footer, font=small)
        tw = bbox[2] - bbox[0]
    except Exception:
        tw = len(footer) * 10
    draw.text(((w - tw)//2, h - 120), footer, font=small, fill=(180, 180, 180))
    img.save(out_path, quality=92)
    return out_path

def _ensure_frame(shot: dict, asset: dict | None, run_dir: Path, tmp_dir: Path) -> Path:
    """Stelle sicher dass ein Frame existiert (asset file oder synthetic)."""
    idx = shot.get("idx")
    # title_card synthetic
    if shot.get("asset_type") == "title_card" or (asset and asset.get("status") == "synthetic"):
        # generate title card from subject or story text
        subject = shot.get("subject", "Apple News")
        # try to use shot description or subject
        text = shot.get("description", subject)[:80]
        # for intro/outro use shot subject
        out = tmp_dir / f"frame_{idx:02d}.jpg"
        _generate_title_card(text, out)
        return out

    # try asset file
    if asset and asset.get("status") == "found" and asset.get("file_path"):
        src = run_dir / asset["file_path"]
        if src.exists():
            # prepare to 9:16 via prepare_source_frame
            out = tmp_dir / f"frame_{idx:02d}.jpg"
            try:
                # use broll's prepare to do blur+contain
                fitted = tmp_dir / f"frame_{idx:02d}_fit.jpg"
                prepare_source_frame(src, fitted)
                # fitted is already 1080x1920, but animate expects that
                # copy fitted to out for animate step (animate will re-save)
                shutil.copy(fitted, out)
                return out
            except Exception as e:
                log(f"Frame prepare failed {src}: {e} -> fallback copy")
                # fallback: just copy and let animate handle scaling
                try:
                    shutil.copy(src, out)
                    return out
                except Exception:
                    pass

    # fallback: missing -> title card with subject
    subject = shot.get("subject", "Apple News")
    out = tmp_dir / f"frame_{idx:02d}.jpg"
    _generate_title_card(subject, out)
    log(f"Frame {idx} missing -> synthetic title card")
    return out

def render_video(run_dir: Path, lang: str = "en") -> dict:
    """Haupteintrag: rendern."""
    run_dir = Path(run_dir)
    shots_path = run_dir / "shots.json"
    if not shots_path.exists():
        shots_path = run_dir / "visual.json"
    if not shots_path.exists():
        raise FileNotFoundError(f"shots.json fehlt in {run_dir}")
    shots_data = json.loads(shots_path.read_text())
    shots = shots_data.get("shots", [])
    if not shots:
        raise ValueError("Keine shots")

    # assets
    assets_path = run_dir / "assets.json"
    assets = []
    if assets_path.exists():
        assets = json.loads(assets_path.read_text())
    amap = {a.get("shot_idx"): a for a in assets}

    # voiceover
    voice_path = run_dir / "voiceover.mp3"
    if not voice_path.exists():
        # fallback voiceover_en.mp3
        voice_path = run_dir / f"voiceover_{lang}.mp3"
    if not voice_path.exists():
        raise FileNotFoundError(f"voiceover fehlt in {run_dir}")

    duration = get_audio_duration(voice_path)
    # cap 60 for Shorts, but allow up to 180 for new format (ponytail: cap 60)
    if duration > 60:
        log(f"Voiceover {duration:.1f}s >60s -> cappe auf 60s")
        duration = 60.0

    tmp_dir = run_dir / "render_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # captions via Whisper (optional, non-blocking)
    ass_path = None
    srt_path = None
    try:
        from .captions import _whisper_word_timestamps, _generate_ass, _generate_srt
        from .niche import load_niche, get_caption_config
        # try to load niche from run_dir name
        niche = "apple"
        parts = run_dir.name.split("_")
        if len(parts) >= 3 and parts[-2] in ["apple", "tech", "selfhosting"]:
            niche = parts[-2]
        try:
            cfg = get_caption_config(load_niche(niche))
        except Exception:
            cfg = {}
        words = _whisper_word_timestamps(voice_path, lang=lang)
        if words:
            srt_path = tmp_dir / "captions.srt"
            ass_path = tmp_dir / "captions.ass"
            _generate_srt(words, srt_path, group_size=int(cfg.get("words_per_group", 4)))
            _generate_ass(words, ass_path, highlight_color=cfg.get("highlight_color", "#00FF88"), group_size=int(cfg.get("words_per_group", 4)), font_family="DejaVu Sans", font_size=72)
            # copy srt to run_dir
            shutil.copy(srt_path, run_dir / "final.srt")
            log(f"Captions: {len(words)} Worte")
        else:
            log("Keine Whisper-Worte -> ohne Captions")
    except Exception as e:
        log(f"Whisper fehlgeschlagen: {e} -> ohne Captions")
        ass_path = None

    # prepare frames
    frames = []
    for shot in shots:
        idx = shot.get("idx")
        asset = amap.get(idx)
        frame = _ensure_frame(shot, asset, run_dir, tmp_dir)
        frames.append((shot, frame))

    # animate each frame
    xdur = 0.5
    # sort by idx
    frames_sorted = sorted(frames, key=lambda x: x[0].get("idx"))
    n = len(frames_sorted)
    # per-shot duration: use shot.duration, but adjust to fit voiceover
    # sum durations from shots, then scale to voiceover duration
    shot_durs = [float(s.get("duration", 12)) for s, _ in frames_sorted]
    total_shot = sum(shot_durs)
    # xfade total lost = (n-1)*xdur, so needed per-shot sum = duration + lost
    total_needed = duration + (n - 1) * xdur
    scale = total_needed / total_shot if total_shot else 1
    anim_durs = [d * scale for d in shot_durs]

    animated = []
    import random
    effects = ["zoom_in", "scale_reveal", "pan_down", "drift", "zoom_out", "pan_up"]
    random.shuffle(effects)
    for i, (shot, frame) in enumerate(frames_sorted):
        anim = tmp_dir / f"anim_{i:02d}.mp4"
        dur = anim_durs[i] + 0.1  # +0.1 for xfade safety
        # use source_subtle for synthetic/title_card? ponytail: title_card static
        effect = "source_subtle" if shot.get("asset_type") == "title_card" else effects[i % len(effects)]
        # title_card should be static
        if shot.get("asset_type") == "title_card":
            effect = "source_subtle"
        animate_frame(frame, anim, dur, effect)
        animated.append(anim)

    # xfade chain
    if n == 1:
        merged = animated[0]
    else:
        inputs = []
        for p in animated:
            inputs += ["-i", str(p)]
        offsets = []
        acc = 0.0
        for k in range(n - 1):
            acc += anim_durs[k] + 0.1
            offsets.append(max(acc - xdur * (k + 1), 0.01))
        transitions = ["fade"] * (n - 1)  # ponytail: simple fade, kein random
        fc = f"[0:v][1:v]xfade=transition={transitions[0]}:duration={xdur}:offset={offsets[0]:.3f}[v1]"
        for i in range(2, n):
            fc += f";[v{i-1}][{i}:v]xfade=transition={transitions[i-1]}:duration={xdur}:offset={offsets[i-1]:.3f}[v{i}]"
        merged = tmp_dir / "merged.mp4"
        run_cmd(["ffmpeg", *inputs, "-filter_complex", fc, "-map", f"[v{n-1}]", "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", str(merged), "-y", "-loglevel", "error"])

    # final mux with voiceover + captions
    final = run_dir / "final.mp4"
    vf = None
    if ass_path and ass_path.exists() and _ffmpeg_has_libass():
        # escape path for ass filter
        esc = str(ass_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        vf = f"ass={esc}"
    cmd = ["ffmpeg", "-i", str(merged), "-i", str(voice_path)]
    if vf:
        cmd += ["-vf", vf]
    # use libx264 if vf else copy (but we have re-encoded merged, so always re-encode)
    cmd += ["-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", f"{duration:.3f}", str(final), "-y", "-loglevel", "error"]
    # Actually if vf is None, we still need to copy video but merged is already h264, we can copy
    # For simplicity always re-encode if vf else copy? ponytail: always re-encode fast, minimal diff
    if not vf:
        # no burn-in, just mux
        cmd = ["ffmpeg", "-i", str(merged), "-i", str(voice_path), "-c:v", "copy", "-c:a", "aac", "-t", f"{duration:.3f}", str(final), "-y", "-loglevel", "error"]
        # but merged is already h264, copy is fine
        # need to handle case where merged was single file copy? keep copy
        pass
    # choose correct cmd based on vf
    if vf:
        cmd = ["ffmpeg", "-i", str(merged), "-i", str(voice_path), "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", f"{duration:.3f}", str(final), "-y", "-loglevel", "error"]
    else:
        cmd = ["ffmpeg", "-i", str(merged), "-i", str(voice_path), "-c:v", "copy", "-c:a", "aac", "-t", f"{duration:.3f}", str(final), "-y", "-loglevel", "error"]
    run_cmd(cmd)
    log(f"Render: {final} ({duration:.1f}s, {n} shots)")

    # cleanup tmp
    try:
        shutil.rmtree(tmp_dir)
    except Exception:
        pass

    return {
        "final_path": str(final.relative_to(run_dir)) if final.is_relative_to(run_dir) else str(final),
        "duration": round(duration, 2),
        "shots": n,
        "has_captions": ass_path is not None and Path(ass_path).exists(),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

def save_render(run_dir: Path, meta: dict):
    import json
    run_dir = Path(run_dir)
    out = run_dir / "render.json"
    out.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Saved render → {out}")
