"""Run persistence — runs/<ts>/articles.json + sources.json + meta.json"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
import hashlib

from ..log import log


def create_run_dir(runs_dir: Path, niche: str) -> Path:
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    job = str(int(time.time()))
    # ponytail: collision-safe dir name
    name = f"{ts}_{niche}_{job}"
    run_dir = runs_dir / name
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "raw").mkdir(exist_ok=True)
    return run_dir


def save_articles(run_dir: Path, articles: list[dict]):
    run_dir = Path(run_dir)
    # sort newest first for readability
    articles_sorted = sorted(articles, key=lambda a: a.get("published_at") or "", reverse=True)
    out = run_dir / "articles.json"
    out.write_text(json.dumps(articles_sorted, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Saved {len(articles_sorted)} articles → {out}")


def save_sources(run_dir: Path, feed_results: list[dict], niche: str, limit: int):
    run_dir = Path(run_dir)
    started_at = min((r.get("fetched_at") for r in feed_results if r.get("fetched_at")), default=datetime.now(timezone.utc).isoformat().replace("+00:00","Z"))
    finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    total_fetched = sum(r.get("entries_fetched", 0) for r in feed_results)
    total_kept = sum(r.get("entries_kept", 0) for r in feed_results)
    # distinct articles after dedup will be less; we report kept before dedup here,
    # caller can compute post-dedup if needed. For Phase 1 we include both.

    # Build per-feed entry without raw_bytes (too large)
    feeds = []
    for r in feed_results:
        feeds.append({
            "url": r.get("feed_url"),
            "feed_title": r.get("feed_title"),
            "status": r.get("status"),
            "http_status": r.get("http_status"),
            "bozo": r.get("bozo"),
            "bozo_exception": (r.get("bozo_exception") or "")[:500] if r.get("bozo_exception") else None,
            "entries_fetched": r.get("entries_fetched"),
            "entries_kept": r.get("entries_kept"),
            "error": r.get("error"),
            "fetched_at": r.get("fetched_at"),
            "duration_ms": r.get("duration_ms"),
            "raw_hash": r.get("raw_hash"),
            "raw_path": r.get("raw_path"),
        })

    sources = {
        "niche": niche,
        "limit": limit,
        "started_at": started_at,
        "finished_at": finished_at,
        "total_feeds": len(feed_results),
        "feeds_ok": sum(1 for r in feed_results if r.get("status") == "ok"),
        "feeds_error": sum(1 for r in feed_results if r.get("status") == "error"),
        "feeds_empty": sum(1 for r in feed_results if r.get("status") == "empty"),
        "entries_fetched": total_fetched,
        "entries_kept": total_kept,
        "feeds": feeds,
    }
    out = run_dir / "sources.json"
    out.write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Saved sources → {out}")

    # meta.json
    meta = {
        "niche": niche,
        "run_dir": str(run_dir),
        "created_at": finished_at,
        "feeds": [r.get("feed_url") for r in feed_results],
        "limit": limit,
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def save_raw(run_dir: Path, feed_url: str, raw_bytes: bytes):
    """Manual raw save helper (normally handled in rss.py)."""
    if not raw_bytes:
        return None
    safe = hashlib.sha256(feed_url.encode()).hexdigest()[:8]
    out = Path(run_dir) / "raw" / f"{safe}.xml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw_bytes)
    return out


def replay_from_raw(run_dir: Path) -> list[dict]:
    """Offline replay: re-parse saved raw/*.xml into articles (no network).

    Returns articles list, does NOT write files (caller decides).
    Used by CLI --replay and tests.
    """
    import feedparser
    from .normalize import normalize_entry

    run_dir = Path(run_dir)
    raw_dir = run_dir / "raw"
    if not raw_dir.exists():
        raise FileNotFoundError(f"No raw dir in {run_dir}")

    # need mapping from raw file to feed_url -> stored in sources.json
    sources_path = run_dir / "sources.json"
    if not sources_path.exists():
        raise FileNotFoundError(f"sources.json missing in {run_dir}")
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    # build hash->url map from feeds entries
    url_by_raw = {}
    for f in sources.get("feeds", []):
        rp = f.get("raw_path")
        if rp:
            url_by_raw[rp] = f.get("url")

    # preserve original fetched_at per feed for deterministic replay
    fetched_at_by_url = {}
    for f in sources.get("feeds", []):
        if f.get("url") and f.get("fetched_at"):
            fetched_at_by_url[f["url"]] = f["fetched_at"]

    articles = []
    for xml_path in sorted(raw_dir.glob("*.xml")):
        rel = f"raw/{xml_path.name}"
        feed_url = url_by_raw.get(rel)
        if not feed_url:
            feed_url = list(url_by_raw.values())[0] if url_by_raw else "unknown"
        raw = xml_path.read_bytes()
        parsed = feedparser.parse(raw)
        fetched_at = fetched_at_by_url.get(feed_url) or datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
        for e in getattr(parsed, "entries", []):
            try:
                art = normalize_entry(e, feed_url, fetched_at)
                if art.get("title") and art.get("url"):
                    articles.append(art)
            except Exception:
                continue
    return articles
