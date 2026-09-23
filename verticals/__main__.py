"""CLI entry point — python -m verticals."""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import CONFIG_FILE, DRAFTS_DIR, MEDIA_DIR, run_setup
from .log import log, set_verbose
from .niche import list_niches


def maybe_run_setup(args):
    """Run first-run setup only for commands that need creator credentials.

    Help, niche listing, topic discovery, and local/free-provider paths should
    not block on an interactive setup wizard.
    """
    if CONFIG_FILE.exists() or args.cmd not in {"draft", "run"}:
        return

    provider = getattr(args, "provider", None)
    if provider in {"ollama", "gemini", "openai"}:
        return

    print("  First run detected. Running setup...")
    run_setup()


def cmd_draft(args):
    from .draft import generate_draft
    from .state import PipelineState
    import json

    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = str(int(time.time()))

    niche = getattr(args, "niche", "general") or "general"
    platform = getattr(args, "platform", "shorts") or "shorts"
    provider = getattr(args, "provider", None)

    print(f"\n  Drafting: {args.news} [niche: {niche}, platform: {platform}]\n")
    draft = generate_draft(
        args.news,
        getattr(args, "context", ""),
        lang=getattr(args, "lang", "en") or "en",
        niche=niche,
        platform=platform,
        provider=provider,
    )
    draft["job_id"] = job_id

    out_path = DRAFTS_DIR / f"{job_id}.json"
    state = PipelineState(draft)
    state.complete_stage("research")
    state.complete_stage("draft")
    state.save(out_path)

    print(f"\n  Draft saved: {out_path}")
    print(f"\n  Script:\n{draft['script']}")
    print(f"\n  Title: {draft.get('youtube_title', '')}")
    print(f"\n  B-roll prompts:")
    for i, p in enumerate(draft.get("broll_prompts", [])):
        print(f"  {i+1}. {p}")

    return out_path


def cmd_produce(args):
    from .broll import generate_broll
    from .tts import generate_voiceover
    from .captions import generate_captions
    from .music import select_and_prepare_music
    from .assemble import assemble_video
    from .niche import load_niche, get_voice_config, get_caption_config, get_music_config
    from .state import PipelineState
    import json
    import shutil

    draft_path = Path(args.draft)
    draft = json.loads(draft_path.read_text())
    job_id = draft["job_id"]
    lang = args.lang
    state = PipelineState(draft)

    # Load niche profile for voice/caption/music config
    niche_name = draft.get("niche", "general")
    profile = load_niche(niche_name)

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    work_dir = MEDIA_DIR / f"work_{job_id}_{lang}"
    work_dir.mkdir(exist_ok=True)

    force = getattr(args, "force", False)
    tts_provider = getattr(args, "voice", None)
    script = getattr(args, "script", None) or (
        draft.get("script_hi") if lang == "hi" else draft.get("script")
    )

    print(f"\n  Producing {lang.upper()} video for job {job_id} [niche: {niche_name}]")

    # B-roll — nutze vorhandene Frames wenn sie existieren (manuell oder Pipeline)
    existing_frames = sorted(work_dir.glob("broll_*.jpg")) + sorted(work_dir.glob("broll_*.png"))
    if existing_frames:
        log(f"Using {len(existing_frames)} existing source frames from work dir")
        frames = existing_frames
    elif force or not state.is_done("broll"):
        est_words = len((script or "").split()) or 150
        target_frames = max(6, min(14, round(est_words / 2.4 / 3.5)))
        frames = generate_broll(draft.get("broll_prompts", ["Cinematic landscape"] * 3),
                                work_dir, target_frames=target_frames, script_text=script)
        state.complete_stage("broll", {"frames": [str(f) for f in frames]})
    else:
        log("Skipping b-roll (already done)")
        frames = [Path(f) for f in state.get_artifact("broll", "frames", [])]

    # Voiceover (niche-aware voice selection)
    if force or not state.is_done("voiceover"):
        voice_config = get_voice_config(
            profile,
            provider=tts_provider or "edge_tts",
            lang=lang,
        )
        vo_path = generate_voiceover(
            script, work_dir, lang,
            provider=tts_provider,
            voice_config=voice_config,
        )
        state.complete_stage("voiceover", {"path": str(vo_path)})
    else:
        log("Skipping voiceover (already done)")
        vo_path = Path(state.get_artifact("voiceover", "path"))

    # Whisper + Captions (niche-aware styling)
    caption_config = get_caption_config(profile)
    if force or not state.is_done("captions"):
        captions_result = generate_captions(
            vo_path, work_dir, lang,
            highlight_color=caption_config.get("highlight_color", "#FFFF00"),
            words_per_group=caption_config.get("words_per_group", 4),
            font_family=caption_config.get("font_family", "Arial"),
            font_size=int(caption_config.get("font_size", 72)),
        )
        state.complete_stage("captions", {
            "srt_path": str(captions_result.get("srt_path", "")),
            "ass_path": str(captions_result.get("ass_path", "")),
        })
    else:
        log("Skipping captions (already done)")
        captions_result = {
            "srt_path": state.get_artifact("captions", "srt_path", ""),
            "ass_path": state.get_artifact("captions", "ass_path", ""),
        }

    # Music (niche-aware mood/ducking)
    music_config = get_music_config(profile)
    if force or not state.is_done("music"):
        music_result = select_and_prepare_music(
            vo_path, work_dir,
            duck_speech=music_config.get("duck_volume_speech", 0.12),
            duck_gap=music_config.get("duck_volume_gap", 0.25),
        )
        state.complete_stage("music", {
            "track_path": str(music_result.get("track_path", "")),
            "duck_filter": music_result.get("duck_filter", ""),
        })
    else:
        log("Skipping music (already done)")
        music_result = {
            "track_path": state.get_artifact("music", "track_path", ""),
            "duck_filter": state.get_artifact("music", "duck_filter", ""),
        }

    # Assemble
    if force or not state.is_done("assemble"):
        video_path = assemble_video(
            frames=frames,
            voiceover=vo_path,
            out_dir=work_dir,
            job_id=job_id,
            lang=lang,
            ass_path=captions_result.get("ass_path"),
            music_path=music_result.get("track_path"),
            duck_filter=music_result.get("duck_filter"),
        )
        state.complete_stage("assemble", {"video_path": str(video_path)})
    else:
        log("Skipping assembly (already done)")
        video_path = Path(state.get_artifact("assemble", "video_path"))

    # Save SRT to media dir
    srt_path = captions_result.get("srt_path")
    if srt_path and Path(srt_path).exists():
        final_srt = MEDIA_DIR / f"verticals_{job_id}_{lang}.srt"
        shutil.copy(srt_path, final_srt)
        draft[f"srt_{lang}"] = str(final_srt)

    draft[f"video_{lang}"] = str(video_path)
    state.save(draft_path)

    print(f"\n  Video: {video_path}")
    return video_path


def cmd_upload(args):
    from .upload import upload_to_youtube
    from .thumbnail import generate_thumbnail
    from .state import PipelineState
    import json

    draft_path = Path(args.draft)
    draft = json.loads(draft_path.read_text())
    lang = args.lang
    state = PipelineState(draft)
    force = getattr(args, "force", False)

    video_path = Path(draft.get(f"video_{lang}", ""))
    srt_path_str = draft.get(f"srt_{lang}")
    srt_path = Path(srt_path_str) if srt_path_str else None

    if not video_path.exists():
        print(f"  No produced video found for lang={lang}. Run produce first.")
        sys.exit(1)

    # Thumbnail
    thumb_path = None
    if force or not state.is_done("thumbnail"):
        try:
            thumb_path = generate_thumbnail(draft, MEDIA_DIR)
            state.complete_stage("thumbnail", {"path": str(thumb_path)})
        except Exception as e:
            log(f"Thumbnail generation failed: {e} — uploading without thumbnail")
    else:
        thumb_p = state.get_artifact("thumbnail", "path", "")
        if thumb_p and Path(thumb_p).exists():
            thumb_path = Path(thumb_p)

    # Upload
    if force or not state.is_done("upload"):
        url = upload_to_youtube(video_path, draft, srt_path, lang, thumb_path)
        state.complete_stage("upload", {"url": url})
    else:
        url = state.get_artifact("upload", "url", "")
        log(f"Skipping upload (already done): {url}")

    draft[f"youtube_url_{lang}"] = url
    state.save(draft_path)
    print(f"\n  Live: {url}")
    # prune work dir nach erfolgreichem Upload (ponytail: sofort, nicht 7d pöbeln)
    if url and "youtu.be" in url:
        try:
            job_id = draft.get("job_id", "")
            if job_id:
                from .cleanup import prune_after_upload
                work_dir = MEDIA_DIR / f"work_{job_id}_{lang}"
                prune_after_upload(work_dir)
                # _en_en variant
                alt = MEDIA_DIR / f"work_{job_id}_{lang}_en"
                if alt.exists():
                    prune_after_upload(alt)
        except Exception as e:
            log(f"Prune nach Upload übersprungen: {e}")
    return url


def cmd_daily(args):
    """Daily pipeline: discover trending topic → draft → produce → share."""
    from .topics import TopicEngine
    import shutil

    niche = getattr(args, "niche", "selfhosting") or "selfhosting"
    lang = getattr(args, "lang", "en") or "en"

    log(f"=== Daily pipeline [{niche}] ===")

    # 1. Discover trending topics
    engine = TopicEngine(niche=niche)
    candidates = engine.discover(limit=20)
    if not candidates:
        log("ERROR: no topics found")
        sys.exit(1)

    log(f"Found {len(candidates)} topics, picking best...")

    # Filter: nur Self-Hosting-relevante Topics (weich: bevorzugen, nicht ausschließen)
    # Früher wurden nicht passende Topics komplett verworfen → zu oft "no relevant".
    # Jetzt: passende zuerst sortieren, aber ALLE Themen bleiben Kandidaten.
    hs_kw = {"server","docker","homelab","selfhost","proxmox","nas","raid","zfs",
             "vpn","firewall","container","kubernetes","k8s","pihole","adguard",
             "tailscale","wireguard","nginx","traefik","linux","debian","ubuntu",
             "synology","unraid","truenas","homeassistant","homeautomation",
             "ollama","llm","ai","backup","restic","paperless","n8n","nextcloud",
             "gitlab","gitea","jenkins","monitoring","grafana","prometheus",
             "virtualization","vm","hypervisor","network","dns","ssl","tls",
             "ssh","git","cli","terminal","open","source","foss","privacy",
             "security","encrypt","password","bitwarden","podman","lxc"}
    ranked = sorted(candidates, key=lambda c: any(w in c.title.lower() for w in hs_kw), reverse=True)
    # Deduplizierung: Topics der letzten 7 Tage nicht wiederholen
    # (verhindert "Ex-FTC..." an zwei Tagen hintereinander + Endlosschleife
    # auf demselben RSS-Artikel, wenn der Draft einmal abraucht).
    try:
        import re as _re
        recent_titles: list[str] = []
        for _p in sorted(DRAFTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:14]:
            try:
                _d = json.loads(_p.read_text())
                for _k in ("news", "youtube_title"):
                    if _d.get(_k):
                        recent_titles.append(str(_d[_k]))
            except Exception:
                continue

        def _words(t: str) -> set[str]:
            return set(w for w in _re.findall(r"[a-z0-9]{4,}", t.lower()) if w not in hs_kw) or set(
                _re.findall(r"[a-z0-9]{4,}", t.lower())
            )

        def _is_repeat(title: str) -> bool:
            tw = _words(title)
            if not tw:
                return False
            for rt in recent_titles:
                rw = _words(rt)
                if not rw:
                    continue
                # identisch oder starke Wortueberlappung => Wiederholung
                if title.strip().lower() == rt.strip().lower():
                    return True
                inter = len(tw & rw) / max(len(tw | rw), 1)
                if inter > 0.5:
                    return True
            return False

        fresh = [c for c in ranked if not _is_repeat(c.title)]
        if fresh:
            if len(fresh) < len(ranked):
                log(f"Dedup: {len(ranked) - len(fresh)} wiederholte Topics uebersprungen")
            ranked = fresh
        else:
            log("Dedup: alle Kandidaten bereits kuerzlich verwendet — nutze trotzdem neuestes")
    except Exception as e:
        log(f"Dedup übersprungen: {e}")
    best = ranked[0]
    topic = best.title
    log(f"Selected: {topic} (Quelle: {best.source}, URL: {best.url or 'keine'})")

    # 2. Draft
    draft_args = argparse.Namespace(
        news=topic, niche=niche, lang=lang, provider="gemini",
        context="", dry_run=False, verbose=False
    )
    draft_path = cmd_draft(draft_args)
    draft_data = json.loads(Path(draft_path).read_text())

    # 3. Produce mit Bild-zu-Text-Sync (HN/Reddit/Tool-Website Screenshots)
    from .produce_synced import produce_synced
    video_path = produce_synced(
        topic=topic, niche=niche, lang=lang,
        draft=draft_data, topic_url=best.url,
    )

    # 4. Copy to exchange share
    if video_path and Path(video_path).exists():
        exchange = Path("/srv/docker/hermes/exchange/Sammlung/MoneyMaker")
        exchange.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        dest = exchange / f"daily_{date_str}_{lang}.mp4"
        shutil.copy2(video_path, dest)
        log(f"Shared: {dest}")

        # Save draft with video path
        draft = json.loads(Path(draft_path).read_text())
        draft[f"video_{lang}"] = str(dest)
        Path(draft_path).write_text(json.dumps(draft, indent=2, ensure_ascii=False))

        return dest
    else:
        log("ERROR: no video produced")
        sys.exit(1)


def cmd_run(args):
    draft_path = cmd_draft(args)
    if args.dry_run:
        print("  Dry run — skipping produce + upload")
        return

    class ProduceArgs:
        draft = str(draft_path)
        lang = args.lang
        script = None
        force = False
        voice = getattr(args, "voice", None)

    video_path = cmd_produce(ProduceArgs())

    class UploadArgs:
        draft = str(draft_path)
        lang = args.lang
        force = False

    url = cmd_upload(UploadArgs())
    print(f"\n  Done! {url}")


def cmd_topics(args):
    from .topics import TopicEngine

    niche = getattr(args, "niche", "general") or "general"
    engine = TopicEngine(niche=niche)
    candidates = engine.discover(limit=getattr(args, "limit", 15))

    if not candidates:
        print("  No topics found from enabled sources.")
        return

    print(f"\n  Trending topics for [{niche}] ({len(candidates)} found):\n")
    for i, topic in enumerate(candidates, 1):
        score = f" [{topic.trending_score:.2f}]" if topic.trending_score else ""
        print(f"  {i:2d}. [{topic.source}] {topic.title}{score}")
        if topic.summary:
            print(f"      {topic.summary[:100]}")


def cmd_niches(args):
    """List all available niche profiles."""
    niches = list_niches()
    print(f"\n  Available niches ({len(niches)}):\n")
    for n in niches:
        from .niche import load_niche
        profile = load_niche(n)
        display = profile.get("display_name", n)
        desc = profile.get("description", "")[:80]
        print(f"    {n:20s}  {display}")
        if desc:
            print(f"    {' ':20s}  {desc}")


def cmd_prune(args):
    """Prune old work/media/exchange files (nach Upload + 7d/14d)."""
    from .cleanup import prune_all, prune_after_upload
    import argparse
    if getattr(args, "work_dir", None):
        # single work dir after upload
        ok = prune_after_upload(Path(args.work_dir), dry_run=getattr(args, "dry_run", False))
        sys.exit(0 if ok else 1)
    # full prune
    res = prune_all(dry_run=getattr(args, "dry_run", False))
    print(f"\n  Prune {'(dry)' if args.dry_run else ''}: {res['total_freed_mb']:.1f} MB frei")
    for k in ("work", "media", "exchange"):
        d = res[k]["deleted"]
        if d:
            print(f"  {k}: {len(d)} gelöscht")

def cmd_editorial(args):
    """Editorial selection: LL M wählt 3-5 Stories aus Pool (Phase 3)."""
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    from .config import RUNS_DIR
    from .editorial import select_editorial

    # resolve run_dir: explicit or latest
    run_dir = getattr(args, "run_dir", None)
    if run_dir:
        run_dir = Path(run_dir)
    else:
        # latest in RUNS_DIR (or --out override)
        base = Path(getattr(args, "out", None)) if getattr(args, "out", None) else RUNS_DIR
        if args.niche:
            # prefer niche-specific latest: filter by name containing niche
            candidates = sorted(base.glob(f"*_{args.niche}_*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not candidates:
                candidates = sorted(base.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        else:
            candidates = sorted(base.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            print(f"  Kein Run gefunden in {base} — erst ingest ausführen")
            sys.exit(1)
        run_dir = candidates[0]

    if not (run_dir / "articles.json").exists():
        print(f"  articles.json fehlt in {run_dir}")
        sys.exit(1)

    articles = json.loads((run_dir / "articles.json").read_text())
    community = []
    comm_path = run_dir / "community.json"
    if comm_path.exists():
        try:
            comm_data = json.loads(comm_path.read_text())
            community = comm_data.get("signals", [])
        except Exception:
            community = []
    # allow override via args.with_reddit? always use stored community
    edition = getattr(args, "edition", None)
    if not edition:
        # use run's started_at or today
        try:
            meta = json.loads((run_dir / "meta.json").read_text())
            edition = meta.get("created_at", "")[:10]
        except Exception:
            edition = None
        if not edition or not edition.startswith("20"):
            edition = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    provider = getattr(args, "provider", None)
    print(f"\n  Editorial für {run_dir.name} — {len(articles)} Artikel, {len(community)} Reddit-Signale, Edition {edition}, Provider {provider or 'auto'}")
    result = select_editorial(articles, community, edition=edition, provider=provider)

    # QC: validate already done in select_editorial, but save QC info
    qc_ok = "_fallback" not in result
    # persist
    out_path = run_dir / "editorial.json"
    # also save raw for debugging
    to_save = dict(result)
    # keep _llm_raw truncated; also save metrics
    to_save["_meta"] = {
        "run_dir": str(run_dir),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "provider": provider or "auto",
        "articles_count": len(articles),
        "community_count": len(community),
        "qc_pass": qc_ok,
    }
    out_path.write_text(json.dumps(to_save, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Editorial → {out_path} ({len(result['stories'])} stories, qc={'pass' if qc_ok else 'fallback'})")
    for s in result["stories"]:
        print(f"    {s['story_id']}: [{s['status']}] {s['importance']:.2f} {s['headline'][:70]}  sources={','.join(s['sources'])}")
        print(f"      → {s['reason'][:100]}")
    if not qc_ok:
        print(f"  WARN: Fallback verwendet ({result.get('_error','')})")
    return out_path

def cmd_script(args):
    """Script: gesprochenes Briefing aus Editorial (Phase 4)."""
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    from .config import RUNS_DIR
    from .script import generate_script

    run_dir = getattr(args, "run_dir", None)
    if run_dir:
        run_dir = Path(run_dir)
    else:
        base = Path(getattr(args, "out", None)) if getattr(args, "out", None) else RUNS_DIR
        if args.niche:
            candidates = sorted(base.glob(f"*_{args.niche}_*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not candidates:
                candidates = sorted(base.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        else:
            candidates = sorted(base.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            print(f"  Kein Run gefunden in {base} — erst ingest+editorial")
            sys.exit(1)
        run_dir = candidates[0]

    if not (run_dir / "editorial.json").exists():
        print(f"  editorial.json fehlt in {run_dir} — erst editorial ausführen")
        sys.exit(1)
    if not (run_dir / "articles.json").exists():
        print(f"  articles.json fehlt in {run_dir}")
        sys.exit(1)

    editorial = json.loads((run_dir / "editorial.json").read_text())
    # strip _meta/_llm_raw for generation
    editorial_clean = {k: v for k, v in editorial.items() if not k.startswith("_")}
    articles = json.loads((run_dir / "articles.json").read_text())
    niche = getattr(args, "niche", None) or editorial.get("_meta", {}).get("niche") or "apple"
    # try to get niche from run_dir name
    if not niche or niche == "general":
        # parse niche from run_dir like 2026-09-22T..._apple_...
        parts = run_dir.name.split("_")
        if len(parts) >= 3:
            niche = parts[-2] if parts[-2] in ["apple", "general", "tech", "selfhosting"] else "apple"

    provider = getattr(args, "provider", None)
    print(f"\n  Script für {run_dir.name} — {len(editorial_clean.get('stories',[]))} Stories, Niche {niche}, Provider {provider or 'auto'}")
    result = generate_script(editorial_clean, articles, edition=editorial_clean.get("edition"), niche=niche, provider=provider)

    qc_ok = "_fallback" not in result
    out_path = run_dir / "script.json"
    to_save = dict(result)
    to_save["_meta"] = {
        "run_dir": str(run_dir),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "provider": provider or "auto",
        "niche": niche,
        "qc_pass": qc_ok,
        "word_count": result.get("word_count", 0),
    }
    out_path.write_text(json.dumps(to_save, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Script → {out_path} ({result.get('word_count',0)} Worte, qc={'pass' if qc_ok else 'fallback'})")
    print(f"  Intro: {result.get('intro','')[:100]}")
    for s in result.get("stories", []):
        print(f"    {s['story_id']}: {s['text'][:80]} ({s.get('duration_target',0)}s)")
    print(f"  Outro: {result.get('outro','')[:100]}")
    if not qc_ok:
        print(f"  WARN: Fallback ({result.get('_error','')})")
    return out_path

def cmd_visual(args):
    """Visual Plan: Shotlist ohne URLs (Phase 5)."""
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    from .config import RUNS_DIR
    from .visual import generate_visual_plan

    run_dir = getattr(args, "run_dir", None)
    if run_dir:
        run_dir = Path(run_dir)
    else:
        base = Path(getattr(args, "out", None)) if getattr(args, "out", None) else RUNS_DIR
        if args.niche:
            candidates = sorted(base.glob(f"*_{args.niche}_*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not candidates:
                candidates = sorted(base.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        else:
            candidates = sorted(base.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            print(f"  Kein Run gefunden in {base} — erst ingest+editorial+script")
            sys.exit(1)
        run_dir = candidates[0]

    if not (run_dir / "script.json").exists():
        print(f"  script.json fehlt in {run_dir} — erst script ausführen")
        sys.exit(1)
    if not (run_dir / "editorial.json").exists():
        print(f"  editorial.json fehlt in {run_dir}")
        sys.exit(1)

    script = json.loads((run_dir / "script.json").read_text())
    editorial = json.loads((run_dir / "editorial.json").read_text())
    script_clean = {k: v for k, v in script.items() if not k.startswith("_")}
    editorial_clean = {k: v for k, v in editorial.items() if not k.startswith("_")}
    niche = getattr(args, "niche", None) or script.get("_meta", {}).get("niche") or "apple"
    if not niche or niche == "general":
        parts = run_dir.name.split("_")
        if len(parts) >= 3:
            niche = parts[-2] if parts[-2] in ["apple", "general", "tech", "selfhosting"] else "apple"
    provider = getattr(args, "provider", None)
    print(f"\n  Visual für {run_dir.name} — {len(script_clean.get('stories',[]))} Stories, Niche {niche}, Provider {provider or 'auto'}")
    result = generate_visual_plan(script_clean, editorial_clean, edition=script_clean.get("edition"), niche=niche, provider=provider)
    qc_ok = "_fallback" not in result
    out_path = run_dir / "shots.json"
    # also alias visual.json for spec
    to_save = dict(result)
    to_save["_meta"] = {
        "run_dir": str(run_dir),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "provider": provider or "auto",
        "niche": niche,
        "qc_pass": qc_ok,
        "shots_count": len(result.get("shots", [])),
    }
    out_path.write_text(json.dumps(to_save, indent=2, ensure_ascii=False), encoding="utf-8")
    # alias
    (run_dir / "visual.json").write_text(json.dumps(to_save, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Visual → {out_path} ({len(result.get('shots',[]))} shots, qc={'pass' if qc_ok else 'fallback'})")
    for sh in result.get("shots", []):
        print(f"    {sh['idx']:2}: [{sh['story_id']}] {sh['duration']}s {sh['subject'][:30]:30} {sh['asset_type']:22} {sh['preferred_source']}")
        print(f"         → {sh['description'][:80]}")
    if not qc_ok:
        print(f"  WARN: Fallback ({result.get('_error','')})")
    return out_path

def cmd_asset(args):
    """Asset Resolver Phase 6: shots -> assets (apple.com first, kein Stock)."""
    import json
    from pathlib import Path
    from .config import RUNS_DIR
    from .asset_resolver import resolve_assets, save_assets

    run_dir = getattr(args, "run_dir", None)
    if run_dir:
        run_dir = Path(run_dir)
    else:
        base = Path(getattr(args, "out", None)) if getattr(args, "out", None) else RUNS_DIR
        if args.niche:
            candidates = sorted(base.glob(f"*_{args.niche}_*"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not candidates:
                candidates = sorted(base.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        else:
            candidates = sorted(base.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            print(f"  Kein Run gefunden in {base}")
            sys.exit(1)
        run_dir = candidates[0]

    if not (run_dir / "shots.json").exists():
        # fallback visual.json
        if (run_dir / "visual.json").exists():
            # copy to shots.json alias
            import shutil
            shutil.copy(run_dir / "visual.json", run_dir / "shots.json")
        else:
            print(f"  shots.json fehlt in {run_dir} — erst visual ausführen")
            sys.exit(1)
    if not (run_dir / "editorial.json").exists() or not (run_dir / "articles.json").exists():
        print(f"  editorial/articles fehlt in {run_dir}")
        sys.exit(1)

    shots_data = json.loads((run_dir / "shots.json").read_text())
    editorial = json.loads((run_dir / "editorial.json").read_text())
    editorial_clean = {k: v for k, v in editorial.items() if not k.startswith("_")}
    articles = json.loads((run_dir / "articles.json").read_text())

    print(f"\n  Asset Resolver für {run_dir.name} — {len(shots_data.get('shots',[]))} shots")
    assets = resolve_assets(shots_data, editorial_clean, articles, run_dir)
    save_assets(run_dir, assets)
    found = len([a for a in assets if a["status"] == "found"])
    missing = len([a for a in assets if a["status"] == "missing"])
    synthetic = len([a for a in assets if a["status"] == "synthetic"])
    print(f"  Assets → {run_dir / 'assets.json'} ({found} found, {synthetic} synthetic, {missing} missing)")
    for a in assets:
        mark = {"found": "✓", "missing": "✗", "synthetic": "○"}.get(a["status"], "?")
        print(f"    [{mark}] {a['shot_idx']:2} [{a['story_id']}] {a['asset_type']:22} {a['preferred_source']:12} -> {a['status']:9} {a['url'] or ''[:50]} {a.get('width') or ''}x{a.get('height') or ''}")
    return run_dir / "assets.json"

def cmd_ingest(args):
    """RSS (+Reddit Phase2) → normalized articles → runs/<ts>/ persistence."""
    import json
    from pathlib import Path
    from .config import RUNS_DIR
    from .ingest.rss import fetch_all_feeds
    from .ingest.reddit import fetch_reddit_signals
    from .ingest.normalize import dedup_articles
    from .ingest.store import create_run_dir, save_articles, save_sources, save_community, replay_from_raw

    # --replay mode (offline, no network)
    if getattr(args, "replay", None):
        replay_dir = Path(args.replay)
        if not replay_dir.exists():
            print(f"  Replay dir not found: {replay_dir}")
            sys.exit(1)
        try:
            articles = replay_from_raw(replay_dir)
            articles = dedup_articles(articles)
            limit = getattr(args, "limit", None)
            if limit is None:
                try:
                    src = json.loads((replay_dir / "sources.json").read_text())
                    limit = int(src.get("limit", 20))
                except Exception:
                    limit = 20
            articles = sorted(articles, key=lambda a: a.get("published_at") or "", reverse=True)[:limit]
            print(f"\n  Replay from {replay_dir}")
            print(f"  Re-parsed {len(articles)} articles (offline, limit {limit})")
            for i, a in enumerate(articles[:5], 1):
                print(f"  {i}. [{a['source']}] {a['title'][:80]}")
            # also replay community if exists
            comm_path = replay_dir / "community.json"
            if comm_path.exists():
                try:
                    comm = json.loads(comm_path.read_text())
                    print(f"  Community: {len(comm.get('signals', []))} signals (reddit_success={comm.get('reddit_success')})")
                except Exception:
                    pass
            if getattr(args, "out", None):
                out_dir = Path(args.out)
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "articles.json").write_text(json.dumps(articles, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"  Saved to {out_dir / 'articles.json'}")
            return replay_dir
        except Exception as e:
            print(f"  Replay failed: {e}")
            import traceback; traceback.print_exc()
            sys.exit(1)

    niche = getattr(args, "niche", "apple") or "apple"
    limit = getattr(args, "limit", None)
    if limit is None:
        limit = 20
    runs_dir = Path(getattr(args, "out", None)) if getattr(args, "out", None) else RUNS_DIR
    with_reddit = bool(getattr(args, "with_reddit", False))
    reddit_limit = getattr(args, "reddit_limit", 10) or 10

    from .niche import load_niche, get_discovery_config
    profile = load_niche(niche)
    discovery = get_discovery_config(profile)
    feeds = discovery.get("rss") or []
    if not feeds:
        print(f"  No RSS feeds in niche '{niche}' (niches/{niche}.yaml discovery.rss)")
        sys.exit(1)

    print(f"\n  Ingesting [{niche}] — {len(feeds)} feeds, limit {limit}" + (" + Reddit" if with_reddit else ""))
    for f in feeds:
        print(f"    • {f}")
    if with_reddit:
        reddit_subs = discovery.get("reddit") or []
        if reddit_subs:
            print(f"  Reddit: {', '.join(reddit_subs)} (limit {reddit_limit}, non-blocking)")

    run_dir = create_run_dir(runs_dir, niche)
    print(f"\n  Run dir: {run_dir}")

    feed_results = fetch_all_feeds(feeds, limit=limit, run_dir=run_dir)

    for r in feed_results:
        status = r.get("status")
        mark = {"ok": "✓", "error": "✗", "empty": "○"}.get(status, "?")
        print(f"  [{mark}] {r.get('feed_url')[:60]:60}  {r.get('entries_fetched'):2} fetched → {r.get('entries_kept'):2} kept  ({r.get('duration_ms')}ms)  status={status}" + (f"  error={r.get('error')}" if r.get("error") else ""))

    all_articles = []
    for fr in feed_results:
        all_articles.extend(fr.get("articles", []))
    kept_before = len(all_articles)
    deduped = dedup_articles(all_articles)
    deduped = sorted(deduped, key=lambda a: a.get("published_at") or "", reverse=True)[:limit]

    save_articles(run_dir, deduped)
    save_sources(run_dir, feed_results, niche=niche, limit=limit)

    # Reddit optional Phase 2
    reddit_results = []
    community_signals = []
    if with_reddit:
        reddit_subs = discovery.get("reddit") or []
        if not reddit_subs and niche == "apple":
            reddit_subs = ["apple", "iphone"]
        if reddit_subs:
            print(f"\n  Reddit ingest ({len(reddit_subs)} subs)...")
            reddit_results = fetch_reddit_signals(reddit_subs, reddit_limit=reddit_limit, run_dir=run_dir)
            for r in reddit_results:
                community_signals.extend(r.get("signals", []))
                status = r.get("status")
                mark = {"ok": "✓", "rate_limited": "◷", "error": "✗", "empty": "○"}.get(status, "?")
                print(f"  [{mark}] r/{r.get('subreddit'):15} {r.get('signals_fetched'):2} signals  status={status}" + (f"  {r.get('error')}" if r.get("error") else ""))
            # global trim
            community_signals = sorted(community_signals, key=lambda s: s.get("published_at") or "", reverse=True)[:reddit_limit]
            save_community(run_dir, reddit_results, community_signals, niche=niche)
            print(f"  Community: {len(community_signals)} signals kept (limit {reddit_limit})")
        else:
            save_community(run_dir, [], [], niche=niche)
            print("  Reddit: keine Subreddits konfiguriert — skip")
    else:
        # ensure empty community.json for consistency if not with_reddit? ponytail: skip, only when flag
        pass

    print(f"\n  Articles: {kept_before} kept → {len(deduped)} deduped (limit {limit})")
    print(f"  Sources: {run_dir / 'sources.json'}")
    print(f"  Articles: {run_dir / 'articles.json'}")
    if with_reddit:
        print(f"  Community: {run_dir / 'community.json'} ({len(community_signals)} signals)")
    print(f"  Raw: {run_dir / 'raw'} ({len(list((run_dir / 'raw').glob('*.xml')))} files)")
    for i, a in enumerate(deduped[:5], 1):
        print(f"  {i}. [{a['source']}] {a['title'][:80]}  {a.get('published_at','')}")
    if community_signals:
        print("  Reddit top:")
        for i, s in enumerate(community_signals[:3], 1):
            print(f"    {i}. [r/{s['subreddit']}] {s['title'][:70]}")
    return run_dir


def cmd_voices(args):
    """List voices available for a TTS provider.

    Currently only 60db is supported — it exposes GET /myvoices and
    GET /default-voices. Edge TTS voices are language-coded strings (see
    EDGE_VOICES in tts.py); ElevenLabs voice IDs come from the ElevenLabs
    dashboard.
    """
    provider = (args.provider or "").lower()
    if provider not in ("60db", "sixtydb"):
        print("  Error: --provider 60db is the only listing currently supported.")
        print("  Edge voices: see EDGE_VOICES in verticals/tts.py.")
        print("  ElevenLabs voices: https://elevenlabs.io/app/voice-library")
        sys.exit(1)

    import requests
    from .config import get_60db_key

    api_key = get_60db_key()
    if not api_key:
        print("  Error: SIXTYDB_API_KEY not set. Run setup or export the env var.")
        sys.exit(1)

    headers = {"Authorization": f"Bearer {api_key}"}
    endpoints = [
        ("Default voices", "https://api.60db.ai/default-voices"),
        ("My voices",      "https://api.60db.ai/myvoices"),
    ]

    def _print_voice_row(v: dict):
        labels = v.get("labels") or {}
        lang = labels.get("language_name") or labels.get("language") or "?"
        gender = labels.get("gender") or "?"
        accent = labels.get("accent") or "?"
        model = v.get("model") or "?"
        category = v.get("category") or "?"
        name = v.get("name") or "?"
        vid = v.get("voice_id") or "?"
        print(f"    {vid}  {name:18.18}  {lang:10.10}  {gender:6.6}  {accent:10.10}  {model:14.14}  {category}")

    for title, url in endpoints:
        try:
            r = requests.get(url, headers=headers, timeout=30)
        except Exception as exc:
            print(f"\n  {title}: request failed — {exc}")
            continue
        if r.status_code != 200:
            print(f"\n  {title}: HTTP {r.status_code} — {r.text[:120]}")
            continue
        body = r.json()
        items = body.get("data") or []
        print(f"\n  {title} ({len(items)}):")
        if not items:
            print("    (none)")
            continue
        print(f"    {'voice_id':36}  {'name':18}  {'language':10}  {'gender':6}  {'accent':10}  {'model':14}  category")
        print(f"    {'-' * 36}  {'-' * 18}  {'-' * 10}  {'-' * 6}  {'-' * 10}  {'-' * 14}  {'-' * 8}")
        for v in items:
            _print_voice_row(v)


def main():
    parser = argparse.ArgumentParser(
        description="Verticals v3 — AI-Native Vertical Video Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Docs: https://github.com/rushindrasinha/verticals\n"
               "Product: https://verticals.gg",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="cmd")

    # Shared niche/provider args
    niche_help = f"Content niche ({', '.join(list_niches()[:8])}...)"

    # draft
    p_draft = sub.add_parser("draft", help="Generate script + metadata")
    p_draft.add_argument("--topic", "--news", dest="news", required=False, help="Topic/news headline")
    p_draft.add_argument("--context", default="", help="Channel context")
    p_draft.add_argument("--niche", default="general", help=niche_help)
    p_draft.add_argument("--platform", default="shorts", choices=["shorts", "reels", "tiktok", "all"])
    p_draft.add_argument("--provider", default=None, help="LLM: claude, gemini, openai, ollama")
    p_draft.add_argument("--discover", action="store_true", help="Use topic engine")
    p_draft.add_argument("--auto-pick", action="store_true", help="Let LLM pick the best topic")
    p_draft.add_argument("--dry-run", action="store_true", help="Draft only")

    # produce
    p_produce = sub.add_parser("produce", help="Generate video from draft")
    p_produce.add_argument("--draft", required=True)
    p_produce.add_argument("--lang", default="en", choices=["en", "hi", "es", "pt", "de", "fr", "ja", "ko"])
    p_produce.add_argument("--voice", default=None, help="TTS: edge, elevenlabs, 60db, say")
    p_produce.add_argument("--script", default=None, help="Override script text")
    p_produce.add_argument("--force", action="store_true", help="Redo all stages")

    # upload
    p_upload = sub.add_parser("upload", help="Upload to YouTube")
    p_upload.add_argument("--draft", required=True)
    p_upload.add_argument("--lang", default="en", choices=["en", "hi", "es", "pt", "de", "fr", "ja", "ko"])
    p_upload.add_argument("--force", action="store_true", help="Re-upload even if done")

    # run (full pipeline)
    p_run = sub.add_parser("run", help="Full pipeline: draft -> produce -> upload")
    p_run.add_argument("--topic", "--news", dest="news", required=False, help="Topic/news headline")
    p_run.add_argument("--niche", default="general", help=niche_help)
    p_run.add_argument("--platform", default="shorts", choices=["shorts", "reels", "tiktok", "all"])
    p_run.add_argument("--provider", default=None, help="LLM: claude, gemini, openai, ollama")
    p_run.add_argument("--voice", default=None, help="TTS: edge, elevenlabs, 60db, say")
    p_run.add_argument("--lang", default="en", choices=["en", "hi", "es", "pt", "de", "fr", "ja", "ko"])
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--context", default="")
    p_run.add_argument("--discover", action="store_true")
    p_run.add_argument("--auto-pick", action="store_true")

    # topics
    p_topics = sub.add_parser("topics", help="Discover trending topics")
    p_topics.add_argument("--niche", default="general", help=niche_help)
    p_topics.add_argument("--limit", type=int, default=15, help="Max topics to show")

    # niches
    sub.add_parser("niches", help="List available niche profiles")

    # voices
    p_voices = sub.add_parser("voices", help="List TTS voices (currently: 60db)")
    p_voices.add_argument("--provider", default="60db", help="TTS provider (only '60db' supported)")

    p_daily = sub.add_parser("daily", help="Daily: discover topic → draft → produce → share")
    p_daily.add_argument("--lang", default="en", help="Language code")
    p_daily.add_argument("--niche", default="selfhosting", help="Niche profile")

    # ingest (Phase 1+2)
    p_ingest = sub.add_parser("ingest", help="Phase 1-2: RSS (+Reddit) → runs/<ts>/articles.json")
    p_ingest.add_argument("--niche", default="apple", help=niche_help)
    p_ingest.add_argument("--limit", type=int, default=None, help="Max articles after dedup (default 20, replay uses stored limit)")
    p_ingest.add_argument("--out", default=None, help="Runs dir override (default ~/.verticals/runs)")
    p_ingest.add_argument("--replay", default=None, help="Offline replay from existing run dir (no network)")
    p_ingest.add_argument("--with-reddit", action="store_true", help="Reddit-Signal miterfassen (Phase 2, non-blocking)")
    p_ingest.add_argument("--reddit-limit", type=int, default=10, help="Max Reddit-Signale je Run (default 10)")

    # editorial (Phase 3)
    p_editorial = sub.add_parser("editorial", help="Phase 3: Editorial 3-5 Stories aus Pool (LLM)")
    p_editorial.add_argument("--run-dir", default=None, help="Run-Verzeichnis (default: neuester in ~/.verticals/runs)")
    p_editorial.add_argument("--niche", default=None, help="Niche für latest-Fallback (default auto)")
    p_editorial.add_argument("--out", default=None, help="Runs-Basis dir (default ~/.verticals/runs)")
    p_editorial.add_argument("--edition", default=None, help="Edition Datum YYYY-MM-DD (default heute)")
    p_editorial.add_argument("--provider", default=None, help="LLM: gemini, openai, claude, ollama (default auto)")

    # script (Phase 4)
    p_script = sub.add_parser("script", help="Phase 4: Script (gesprochenes Briefing) aus Editorial (LLM)")
    p_script.add_argument("--run-dir", default=None, help="Run-Verzeichnis (default: neuester in ~/.verticals/runs)")
    p_script.add_argument("--niche", default=None, help="Niche für latest-Fallback / Tone (default aus Run)")
    p_script.add_argument("--out", default=None, help="Runs-Basis dir (default ~/.verticals/runs)")
    p_script.add_argument("--provider", default=None, help="LLM: gemini, openai, claude, ollama (default auto)")

    # visual (Phase 5)
    p_visual = sub.add_parser("visual", help="Phase 5: Visual Plan Shotlist ohne URLs (LLM)")
    p_visual.add_argument("--run-dir", default=None, help="Run-Verzeichnis (default: neuester in ~/.verticals/runs)")
    p_visual.add_argument("--niche", default=None, help="Niche für Style (default aus Run)")
    p_visual.add_argument("--out", default=None, help="Runs-Basis dir (default ~/.verticals/runs)")
    p_visual.add_argument("--provider", default=None, help="LLM: gemini, openai, claude, ollama (default auto)")

    # asset (Phase 6)
    p_asset = sub.add_parser("asset", help="Phase 6: Asset Resolver apple.com → missing (kein Stock)")
    p_asset.add_argument("--run-dir", default=None, help="Run-Verzeichnis (default: neuester in ~/.verticals/runs)")
    p_asset.add_argument("--niche", default=None, help="Niche für latest-Fallback (default auto)")
    p_asset.add_argument("--out", default=None, help="Runs-Basis dir (default ~/.verticals/runs)")

    # prune
    p_prune = sub.add_parser("prune", help="Cleanup: work dirs nach Upload + alte media")
    p_prune.add_argument("--work-dir", default=None, help="Einzelnes work_* Verzeichnis nach Upload löschen")
    p_prune.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nicht löschen")

    args = parser.parse_args()

    if args.verbose:
        set_verbose(True)

    if not args.cmd:
        parser.print_help()
        return

    # Handle utility commands that don't need first-run setup
    if args.cmd == "niches":
        cmd_niches(args)
        return
    if args.cmd == "voices":
        cmd_voices(args)
        return
    if args.cmd == "ingest":
        cmd_ingest(args)
        return
    if args.cmd == "prune":
        cmd_prune(args)
        return
    if args.cmd == "editorial":
        cmd_editorial(args)
        return
    if args.cmd == "script":
        cmd_script(args)
        return
    if args.cmd == "visual":
        cmd_visual(args)
        return
    if args.cmd == "asset":
        cmd_asset(args)
        return

    maybe_run_setup(args)

    # Handle --discover flag for draft/run
    if args.cmd in ("draft", "run") and getattr(args, "discover", False):
        from .topics import TopicEngine
        niche = getattr(args, "niche", "general") or "general"
        engine = TopicEngine(niche=niche)
        candidates = engine.discover(limit=15)
        if not candidates:
            print("  No trending topics found. Use --topic instead.")
            sys.exit(1)

        if getattr(args, "auto_pick", False):
            args.news = engine.auto_pick(candidates)
            print(f"  Auto-picked: {args.news}")
        else:
            print("\n  Trending topics:\n")
            for i, t in enumerate(candidates, 1):
                print(f"  {i:2d}. [{t.source}] {t.title}")
            choice = input("\n  Pick a number (or enter custom topic): ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                args.news = candidates[int(choice) - 1].title
            else:
                args.news = choice
    elif args.cmd in ("draft", "run") and not getattr(args, "news", None):
        print("  Error: --topic or --discover required")
        sys.exit(1)

    if args.cmd == "draft":
        cmd_draft(args)
    elif args.cmd == "produce":
        cmd_produce(args)
    elif args.cmd == "upload":
        cmd_upload(args)
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "topics":
        cmd_topics(args)
    elif args.cmd == "daily":
        cmd_daily(args)
    elif args.cmd == "ingest":
        cmd_ingest(args)
    elif args.cmd == "prune":
        cmd_prune(args)
    elif args.cmd == "editorial":
        cmd_editorial(args)
    elif args.cmd == "script":
        cmd_script(args)
    elif args.cmd == "visual":
        cmd_visual(args)
    elif args.cmd == "asset":
        cmd_asset(args)


if __name__ == "__main__":
    main()
