"""Full pipeline: Topic → Script → Source Images → Whisper → Sync → Video."""

import json
import re
import time
import shutil
from pathlib import Path

from .config import MEDIA_DIR, VIDEO_WIDTH, VIDEO_HEIGHT, run_cmd
from .log import log
from .llm import call_llm
from .tts import generate_voiceover
from .source_images import sources_for_topic, fetch_source_images
from .broll import animate_frame, prepare_source_frame
from .assemble import get_audio_duration


def _find_timestamp(search_words: list[str], words: list[dict]) -> float:
    for i in range(len(words) - len(search_words) + 1):
        if all(words[i+j]["word"].lower().rstrip(".,!?") == search_words[j].lower().rstrip(".,!?")
               for j in range(len(search_words))):
            return words[i]["start"]
    return 0.0


def _extract_keywords(text: str) -> list[str]:
    stop = {"the","a","an","is","are","was","were","and","or","but","in","on","at",
            "to","for","of","with","by","from","that","this","it","its","you","your",
            "has","have","had","can","will","just","also","than","so","if","not"}
    # ponytail: keep version numbers for iPhone etc (iphone 18 -> keep 18)
    words = re.findall(r'[A-Za-z]{3,}|\b\d{1,2}\b', text)
    return [w.lower() for w in words if w.lower() not in stop][:5]


def _category_keywords():
    return {
        "chip": ["m6","m5","chip","nanometer","nm","cpu","gpu","neural","ai","processor","core"],
        "mini": ["mini","small","compact","inch","899","desktop","box","tiny"],
        "studio": ["studio","ultra","512","massive","36","80"],
        "price": ["dollar","price","pre-order","ships","september","cost","buy"],
        "connect": ["thunderbolt","wifi","bluetooth","ethernet","cluster","distributed"],
    }


def _find_best_image(keywords: list[str], images: list[Path], used: set) -> Path:
    # Round-Robin: wenn alle Bilder schon vergeben sind, beginnt eine neue
    # Runde (statt wie bisher immer images[0] -> "5x selbes Bild").
    if len(used) >= len(images):
        used.clear()
    cats = _category_keywords()
    best_idx, best_score = 0, -1
    for i, img in enumerate(images):
        if i in used:
            continue
        name = img.stem.lower()
        score = 0
        for kw in keywords:
            for cat, cat_kws in cats.items():
                if kw in cat_kws:
                    if any(ck in name for ck in cat_kws):
                        score += 2
                    else:
                        score += 0.5
        if score > best_score:
            best_score = score
            best_idx = i
    used.add(best_idx)
    return images[best_idx]


def produce_synced(topic: str, niche: str = "selfhosting", lang: str = "en",
                    preloaded_images: list[Path] | None = None,
                    draft: dict | None = None, topic_url: str = "") -> Path | None:
    """Full synced pipeline — images match spoken text."""
    job_id = (draft.get("job_id") if draft else None) or str(int(time.time()))
    work_dir = MEDIA_DIR / f"work_{job_id}_{lang}"
    work_dir.mkdir(parents=True, exist_ok=True)

    # 1. Script: übergebenes Draft nutzen oder kurz selbst generieren
    if draft and draft.get("script"):
        script = draft["script"]
        log(f"Using existing draft script ({len(script.split())} words)")
    else:
        log("Generating short script...")
        script = call_llm(
            f"Write a SHORT YouTube Shorts script (max 100 words) about: {topic}. "
            "Structure: Hook (2 words) → 5 numbered points (1 sentence each) → cliffhanger loop. "
            "Each point must be ONE short sentence. Reply with ONLY the script text.",
            provider="gemini", max_tokens=1500
        )
        job_id = str(int(time.time()))
    # Zu lang? -> Gemini komprimiert auf <=100 Woerter (passt sicher in 60s)
    if len(script.split()) > 115:
        log(f"Script zu lang ({len(script.split())} W) — komprimiere auf <=100...")
        try:
            short = call_llm(
                "Compress this YouTube Shorts script to max 95 words. Keep the hook, "
                "the numbered points structure (First/Second/Third/Fourth/Fifth) and the "
                "closing line. Reply with ONLY the compressed script text:\n\n" + script,
                provider="gemini", max_tokens=1200
            )
            if short and len(short.split()) < len(script.split()):
                script = short.strip()
        except Exception as e:
            log(f"Kompression fehlgeschlagen: {e} — nutze Original")

    words_count = len(script.split())
    log(f"Script: {words_count} words (~{words_count/2.5:.0f}s)")

    # 2. Fetch source images (oder pre-loaded nutzen)
    if preloaded_images:
        images = preloaded_images
        log(f"Using {len(images)} pre-loaded source images")
    else:
        log("Fetching source images...")
        sources = sources_for_topic(topic, topic_url=topic_url)
        images = fetch_source_images(topic, sources, work_dir, n=8)
        log(f"Got {len(images)} source images")

    # 3. Generate voiceover
    log("Generating voiceover...")
    vo_path = generate_voiceover(script, work_dir, lang)
    if not vo_path:
        log("ERROR: voiceover failed")
        return None
    duration = min(get_audio_duration(vo_path), 60.0)
    log(f"Voiceover: {duration:.1f}s")

    # 4. Whisper timestamps
    log("Running Whisper...")
    from .captions import _whisper_word_timestamps as _whisper_timestamps
    words = _whisper_timestamps(vo_path, lang=lang)
    log(f"Got {len(words)} word timestamps")

    # 5. Split script into sections
    section_markers = re.findall(r'(First|Second|Third|Fourth|Fifth|\d+\.)', script, re.IGNORECASE)
    parts = re.split(r'((?:First|Second|Third|Fourth|Fifth|\d+\.)[\s,]*)', script)

    sections = []
    intro_text = parts[0].strip()
    if intro_text:
        sections.append({"text": intro_text, "start": 0.0, "keywords": _extract_keywords(intro_text)})

    for i in range(1, len(parts), 2):
        marker = parts[i].strip()
        text = parts[i+1].strip() if i+1 < len(parts) else ""
        full = (marker + " " + text).strip()
        first_words = full.split()[:3]
        ts = _find_timestamp(first_words, words)
        sections.append({"text": full, "start": ts, "keywords": _extract_keywords(full)})

    # Fallback: Abschnitt ohne Treffer -> interpolieren zwischen Nachbarn
    for i in range(1, len(sections)):
        if sections[i]["start"] <= sections[i-1]["start"]:
            prev_end = sections[i-1]["start"] + 0.5
            nxt = next((s["start"] for s in sections[i+1:] if s["start"] > 0), duration)
            if nxt <= prev_end:
                nxt = duration
            sections[i]["start"] = min(max(prev_end, (prev_end + nxt) / 2), duration - 0.5)

    # Set end times
    for i in range(len(sections)):
        sections[i]["end"] = sections[i+1]["start"] if i+1 < len(sections) else duration

    # 6b. Captions (Mitlese-Text): aus den vorhandenen Whisper-Word-Timestamps
    # SRT + ASS erzeugen — kein extra Whisper-Lauf noetig. Das ASS wird im
    # Final-Mux eingebrannt (libass), das SRT landet im Draft als srt_<lang>
    # und wird von daily_run.sh als YouTube-Untertitel hochgeladen.
    # (Regression 09/2026: produce_synced hatte beides verloren.)
    srt_final: Path | None = None
    ass_path: Path | None = None
    if words:
        try:
            from .captions import _generate_srt, _generate_ass
            from .niche import load_niche as _load_niche, get_caption_config as _get_cap_cfg
            try:
                _cap_cfg = _get_cap_cfg(_load_niche(niche))
            except Exception:
                _cap_cfg = {}
            _srt = work_dir / f"captions_{lang}.srt"
            _ass = work_dir / f"captions_{lang}.ass"
            _generate_srt(words, _srt,
                          group_size=int(_cap_cfg.get("words_per_group", 4)))
            _generate_ass(words, _ass,
                          highlight_color=_cap_cfg.get("highlight_color", "#00FF88"),
                          group_size=int(_cap_cfg.get("words_per_group", 4)),
                          font_family="DejaVu Sans", font_size=72)
            srt_final = MEDIA_DIR / f"verticals_{job_id}_{lang}.srt"
            shutil.copy2(_srt, srt_final)
            ass_path = _ass
            log(f"Captions: {srt_final.name} + burn-in ASS bereit")
        except Exception as e:
            log(f"Captions fehlgeschlagen: {e} — weiter ohne Untertitel")
    else:
        log("Keine Word-Timestamps — keine Untertitel")

    # 6. Match images to sections
    # Topup: fewer images than sections -> stock photos (Openverse) auffuellen,
    # damit nicht 5x dasselbe Bild laeuft (15.09.: nur 2/8 Source-Images).
    if len(images) < len(sections):
        from .broll import fetch_stock_images as _fetch_stock
        missing = len(sections) - len(images)
        kw_pool: list[str] = []
        for sec in sections:
            for kw in _extract_keywords(sec["text"]):
                if kw not in kw_pool:
                    kw_pool.append(kw)
        try:
            extra = _fetch_stock(topic, missing, work_dir, len(images),
                                 keywords=kw_pool[:8] or None)
            images = list(images) + extra
            log(f"Stock-Topup: +{len(extra)} Bilder ({len(images)} total)")
        except Exception as e:
            log(f"Stock-Topup fehlgeschlagen: {e}")
    if len(images) < len(sections):
        # Letzte Reserve: KI-Bilder via Pollinations (kostenlos, keyless)
        from .broll import _generate_image_pollinations as _pollinations
        prompts = (draft.get("broll_prompts", []) if draft else []) or [topic]
        k = 0
        while len(images) < len(sections):
            dst = work_dir / f"pollinations_{len(images)}.jpg"
            try:
                got = _pollinations(prompts[k % len(prompts)], dst)
            except Exception:
                got = None
            if got:
                images = list(images) + [Path(got)]
            else:
                break
            k += 1
            if k > len(sections) + 2:
                break
        log(f"Pollinations-Topup: {len(images)} Bilder total")
    used = set()
    for sec in sections:
        sec["image"] = _find_best_image(sec["keywords"], images, used)
        sec["duration"] = sec["end"] - sec["start"]
        log(f"  [{sec['start']:5.1f}s] {sec['image'].name} — {sec['text'][:50]}...")

    # 7. Animate + assemble
    log("Animating sections...")
    # Shuffle the effect pool per run so consecutive videos use a different
    # camera-move rhythm (avoids the repetitive / templated look on YouTube).
    import random
    effects = ["zoom_in", "scale_reveal", "pan_down", "drift", "zoom_out", "pan_up"]
    random.shuffle(effects)
    xdur = 0.5
    # Nur animierbare Sektionen (>=0.3s); XFade frisst (n-1)*xdur Sekunden —
    # darum jedes Segment (ausser letztem) um +xdur verlaengern, sonst ist das
    # Video kuerzer als der Voiceover und der letzte Satz wird abgeschnitten
    # (15.09.: Video 44.47s < Audio 46.34s -> ~2s Sprache fehlten).
    anim_secs = [sec for sec in sections if sec["duration"] >= 0.3]
    animated = []
    anim_durs: list[float] = []
    for i, sec in enumerate(anim_secs):
        anim = work_dir / f"sync_{i}.mp4"
        seg_dur = sec["duration"] + (xdur if i < len(anim_secs) - 1 else 0.0) + 0.1
        anim_durs.append(seg_dur)
        # Source-Screenshots: lesbar komponieren (Contain+Blur) + subtil animieren.
        # Verhindert abgeschnittene Infos wie am 14.09. ("bs/" statt ganzem Satz).
        try:
            fitted = work_dir / f"sync_{i}_fit.jpg"
            prepare_source_frame(sec["image"], fitted)
            animate_frame(fitted, anim, seg_dur, "source_subtle")
        except Exception as e:
            log(f"Source-Fit fehlgeschlagen ({sec['image'].name}): {e} — Fallback Cover")
            animate_frame(sec["image"], anim, seg_dur, effects[i % len(effects)])
        animated.append(anim)

    if not animated:
        log("ERROR: no animated segments")
        return None

    # XFade chain (bei 1 Segment entfällt der Filter)
    n = len(animated)

    if n == 1:
        merged = animated[0]
    else:
        inputs = []
        for p in animated:
            inputs += ["-i", str(p)]

        # Offsets aus den verlaengerten Segmentdauern: Summe == Voiceover-Dauer
        offsets = []
        acc = 0.0
        for k in range(n - 1):
            acc += anim_durs[k]
            offsets.append(max(acc - xdur * (k + 1), 0.01))

        transitions = random.choices(
            ["fade", "smoothup", "smoothleft", "circleopen", "dissolve"],
            k=n - 1,
        )
        fc = f"[0:v][1:v]xfade=transition={transitions[0]}:duration={xdur}:offset={offsets[0]:.3f}[v1]"
        for i in range(2, n):
            fc += f";[v{i-1}][{i}:v]xfade=transition={transitions[i-1]}:duration={xdur}:offset={offsets[i-1]:.3f}[v{i}]"

        merged = work_dir / "merged_synced.mp4"
        run_cmd(["ffmpeg", *inputs, "-filter_complex", fc, "-map", f"[v{n-1}]",
                 "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
                 str(merged), "-y", "-loglevel", "error"])

    # Final: video + voiceover (+ ASS-Burn-in wenn moeglich)
    out_path = MEDIA_DIR / f"verticals_{job_id}_{lang}.mp4"
    _vf = None
    if ass_path and ass_path.exists():
        from .assemble import _ffmpeg_has_libass
        if _ffmpeg_has_libass():
            _vf = f"ass={str(ass_path).replace(chr(92), chr(92)*2).replace(':', chr(92)+':').replace(chr(39), chr(92)+chr(39))}"
        else:
            log("WARNING: ffmpeg ohne libass — kein Burn-in, SRT geht trotzdem zu YouTube")
    _cmd = ["ffmpeg", "-i", str(merged), "-i", str(vo_path)]
    if _vf:
        _cmd += ["-vf", _vf]
    _cmd += ["-c:v", "libx264" if _vf else "copy"]
    if _vf:
        _cmd += ["-preset", "fast", "-pix_fmt", "yuv420p"]
    _cmd += ["-c:a", "aac", "-t", f"{duration:.3f}",
             str(out_path), "-y", "-loglevel", "quiet"]
    run_cmd(_cmd)

    log(f"Video: {out_path} ({duration:.1f}s)")

    # Save draft
    draft = {
        "news": topic, "script": script, "niche": niche, "platform": "shorts",
        "job_id": job_id,
        "youtube_title": topic[:80],
        "youtube_description": script[:500],
        "youtube_tags": ",".join(set(re.findall(r'[a-zA-Z]{4,}', topic.lower()))),
        f"video_{lang}": str(out_path),
    }
    if srt_final and srt_final.exists():
        draft[f"srt_{lang}"] = str(srt_final)
    draft_path = Path.home() / ".verticals/drafts" / f"{job_id}.json"
    draft_path.write_text(json.dumps(draft, indent=2, ensure_ascii=False))
    log(f"Draft: {draft_path}")

    return out_path
