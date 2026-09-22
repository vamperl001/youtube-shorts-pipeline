"""Pruning — lösche nach erfolgreichem Upload (ponytail: minimal, deterministisch)."""

import time
import shutil
from pathlib import Path
from datetime import datetime, timezone

from .config import MEDIA_DIR, RUNS_DIR
from .log import log


def _age_days(path: Path) -> float:
    try:
        return (time.time() - path.stat().st_mtime) / 86400
    except Exception:
        return 9999


def prune_work_dirs(media_dir: Path | None = None, keep_days: int = 7, keep_last: int = 5, dry_run: bool = False) -> dict:
    """Lösche work_* Verzeichnisse: älter als keep_days UND nicht unter keep_last neuesten.

    Returns {deleted: [Path], kept: [Path], freed_mb: float}
    """
    media_dir = Path(media_dir) if media_dir else MEDIA_DIR
    if not media_dir.exists():
        return {"deleted": [], "kept": [], "freed_mb": 0.0}
    work_dirs = sorted(media_dir.glob("work_*"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    # keep_last newest always kept (even if old)
    to_keep_set = set(work_dirs[:keep_last])
    deleted = []
    kept = []
    freed = 0
    for wd in work_dirs:
        if wd in to_keep_set:
            kept.append(wd)
            continue
        if _age_days(wd) > keep_days:
            # also require that corresponding final video exists? ponytail: age reicht
            try:
                size = sum(f.stat().st_size for f in wd.rglob("*") if f.is_file())
            except Exception:
                size = 0
            if dry_run:
                deleted.append(wd)
                freed += size
            else:
                try:
                    shutil.rmtree(wd)
                    deleted.append(wd)
                    freed += size
                    log(f"Prune work: gelöscht {wd.name} ({size/1024/1024:.1f} MB, {_age_days(wd):.1f}d)")
                except Exception as e:
                    log(f"Prune work fehlgeschlagen {wd}: {e}")
                    kept.append(wd)
        else:
            kept.append(wd)
    return {"deleted": deleted, "kept": kept, "freed_mb": freed / 1024 / 1024}


def prune_after_upload(work_dir: Path | None, dry_run: bool = False) -> bool:
    """Sofort löschen des work_dir nach erfolgreichem Upload (Phase 1 + 2)."""
    if not work_dir:
        return False
    work_dir = Path(work_dir)
    # safety: only delete if it looks like a work dir inside MEDIA_DIR
    try:
        resolved = work_dir.resolve()
        media_resolved = MEDIA_DIR.resolve()
        if media_resolved not in resolved.parents and resolved != media_resolved:
            # also allow work_* directly
            if not work_dir.name.startswith("work_"):
                log(f"Prune after upload: skip {work_dir} (kein work_*)")
                return False
    except Exception:
        pass
    if not work_dir.exists():
        return False
    # check that it is a work dir (contains sync_ / source_ etc) — simple name check
    if not work_dir.name.startswith("work_"):
        log(f"Prune after upload: skip {work_dir} (Name passt nicht)")
        return False
    try:
        size = sum(f.stat().st_size for f in work_dir.rglob("*") if f.is_file())
    except Exception:
        size = 0
    if dry_run:
        log(f"Prune after upload (dry): würde löschen {work_dir.name} ({size/1024/1024:.1f} MB)")
        return True
    try:
        shutil.rmtree(work_dir)
        log(f"Prune after upload: gelöscht {work_dir.name} ({size/1024/1024:.1f} MB)")
        # also delete _en variant if exists (work_xxx_en + work_xxx_en_en)
        for suffix in ["_en", "_en_en"]:
            alt = work_dir.parent / f"{work_dir.name}{suffix}" if not work_dir.name.endswith(suffix) else None
            # Actually work dirs are like work_179..._en — the _en_en is extra
            # Check existing patterns: work_123_en_en
            pass
        # brute: delete any work dir that starts with same job id and is old
        # keep simple: only the one passed
        return True
    except Exception as e:
        log(f"Prune after upload fehlgeschlagen {work_dir}: {e}")
        return False


def prune_top_level_media(media_dir: Path | None = None, keep_days_daily: int = 14, keep_days_verticals: int = 7, dry_run: bool = False) -> dict:
    """Lösche alte daily_final und verticals_*.mp4 (keep recent)."""
    media_dir = Path(media_dir) if media_dir else MEDIA_DIR
    deleted = []
    kept = []
    freed = 0
    # daily_final
    for p in media_dir.glob("daily_final_*.mp4"):
        if _age_days(p) > keep_days_daily:
            try:
                size = p.stat().st_size
                if dry_run:
                    deleted.append(p)
                else:
                    p.unlink()
                    deleted.append(p)
                freed += size
                log(f"Prune media: daily_final gelöscht {p.name} ({size/1024/1024:.1f} MB)")
            except Exception as e:
                log(f"Prune media fehlgeschlagen {p}: {e}")
                kept.append(p)
        else:
            kept.append(p)
    # verticals_*.mp4 (but keep daily_final already handled)
    for p in media_dir.glob("verticals_*.mp4"):
        # don't delete if it's the latest few (keep_last 5)
        # we sort by mtime and keep 5 newest verticals
        pass
    # handle verticals keep_last
    verticals = sorted(media_dir.glob("verticals_*.mp4"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    keep_set = set(verticals[:5])
    for p in verticals:
        if p in keep_set:
            if p not in kept:
                kept.append(p)
            continue
        if _age_days(p) > keep_days_verticals:
            try:
                size = p.stat().st_size
                if dry_run:
                    deleted.append(p)
                else:
                    p.unlink()
                    deleted.append(p)
                freed += size
                log(f"Prune media: verticals gelöscht {p.name} ({size/1024/1024:.1f} MB)")
            except Exception as e:
                log(f"Prune media fehlgeschlagen {p}: {e}")
                kept.append(p)
        else:
            kept.append(p)
    # also prune srt that has no mp4
    for s in media_dir.glob("*.srt"):
        mp4 = s.with_suffix(".mp4")
        # verticals_*.srt -> verticals_*.mp4
        # if mp4 doesn't exist and s is old, delete s
        base = s.name.replace(".srt", "")
        # check if any mp4 with same base exists
        has_video = any((media_dir / f"{base}.mp4").exists() or (media_dir / f"{base}_en.mp4").exists() for base in [base])
        # simplified: if s is old and no mp4, delete
        if not (media_dir / s.with_suffix(".mp4")).exists() and _age_days(s) > keep_days_verticals:
            try:
                size = s.stat().st_size
                if dry_run:
                    deleted.append(s)
                else:
                    s.unlink()
                    deleted.append(s)
                freed += size
            except Exception:
                pass
    return {"deleted": deleted, "kept": kept, "freed_mb": freed / 1024 / 1024}


def prune_exchange(exchange_dir: Path | None = None, dry_run: bool = False) -> dict:
    """Dedup exchange: lösche *_en.mp4 wenn ohne _en existiert (spart 50%, ponytail: Namen zählen)."""
    exchange_dir = Path(exchange_dir) if exchange_dir else Path("/srv/docker/hermes/exchange/Sammlung/MoneyMaker")
    if not exchange_dir.exists():
        return {"deleted": [], "freed_mb": 0.0}
    deleted = []
    freed = 0
    for p in exchange_dir.glob("daily_*_en.mp4"):
        base = p.name.replace("_en.mp4", ".mp4")
        orig = exchange_dir / base
        if orig.exists():
            try:
                size = p.stat().st_size
                if dry_run:
                    deleted.append(p)
                else:
                    p.unlink()
                    deleted.append(p)
                freed += size
                log(f"Prune exchange: Duplikat gelöscht {p.name} ({size/1024/1024:.1f} MB)")
            except Exception as e:
                log(f"Prune exchange fehlgeschlagen {p}: {e}")
    return {"deleted": deleted, "freed_mb": freed / 1024 / 1024}


def prune_all(dry_run: bool = False) -> dict:
    """Gesamt-Prune (work + media + exchange)."""
    r_work = prune_work_dirs(dry_run=dry_run)
    r_media = prune_top_level_media(dry_run=dry_run)
    r_ex = prune_exchange(dry_run=dry_run)
    total_freed = r_work["freed_mb"] + r_media["freed_mb"] + r_ex["freed_mb"]
    log(f"Prune total: {len(r_work['deleted']+r_media['deleted']+r_ex['deleted'])} Dateien, {total_freed:.1f} MB {'(dry)' if dry_run else ''}")
    return {"work": r_work, "media": r_media, "exchange": r_ex, "total_freed_mb": total_freed}
