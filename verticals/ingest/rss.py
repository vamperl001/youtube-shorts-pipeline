"""RSS fetch + parse (deterministic, per-feed metrics + raw persistence)."""

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import re

import requests
import feedparser

from .normalize import normalize_entry
from ..log import log


def _safe_name(url: str) -> str:
    h = hashlib.sha256(url.encode()).hexdigest()[:8]
    host = urlparse(url).netloc.replace(".", "_") or "feed"
    host = re.sub(r"[^A-Za-z0-9_]", "_", host)[:30]
    return f"{host}_{h}"


def fetch_single_feed(feed_url: str, timeout: int = 15) -> dict:
    """Fetch and parse one feed. Returns FeedResult dict."""
    started = time.monotonic()
    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    headers = {"User-Agent": "verticals/1.0 (+rss-ingest)"}
    raw_bytes: bytes | None = None
    status_code: int | None = None
    error: str | None = None
    bozo: bool = False
    bozo_exc: str | None = None

    try:
        r = requests.get(feed_url, headers=headers, timeout=timeout)
        status_code = r.status_code
        raw_bytes = r.content
        if status_code != 200:
            error = f"HTTP {status_code}"
            # try to parse anyway if body present, else return error result
            if not raw_bytes or len(raw_bytes) < 100:
                return {
                    "feed_url": feed_url,
                    "status": "error",
                    "http_status": status_code,
                    "bozo": False,
                    "bozo_exception": None,
                    "entries_fetched": 0,
                    "entries_kept": 0,
                    "error": error,
                    "fetched_at": fetched_at,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "raw_bytes": raw_bytes,
                    "raw_hash": hashlib.sha256(raw_bytes or b"").hexdigest() if raw_bytes else None,
                    "articles": [],
                    "feed_title": None,
                }
        # parse
        # feedparser can parse bytes directly
        parsed = feedparser.parse(raw_bytes)
        bozo = bool(getattr(parsed, "bozo", False))
        if bozo:
            bozo_exc = str(getattr(parsed, "bozo_exception", ""))[:500]
        feed_title = None
        try:
            feed_title = parsed.feed.get("title", "")[:200] if parsed.feed else ""
        except Exception:
            feed_title = None
        entries = getattr(parsed, "entries", []) or []
        articles = []
        for e in entries:
            try:
                art = normalize_entry(e, feed_url, fetched_at)
                # require title and url
                if not art.get("title") or not art.get("url"):
                    continue
                articles.append(art)
            except Exception as exc:
                log(f"normalize failed for {feed_url}: {exc}")
                continue
        duration_ms = int((time.monotonic() - started) * 1000)
        # status logic
        if error:
            status = "error"
        elif not articles and not entries:
            # empty feed: could be bozo or truly empty
            status = "empty" if not bozo else "error"
            if bozo_exc and not error:
                error = bozo_exc[:200]
        elif not articles and entries:
            status = "error"
            error = error or f"all {len(entries)} entries filtered (missing title/url)"
        else:
            status = "ok"
            if bozo:
                # bozo but we got articles -> degraded ok
                status = "ok"

        return {
            "feed_url": feed_url,
            "status": status,
            "http_status": status_code,
            "bozo": bozo,
            "bozo_exception": bozo_exc,
            "entries_fetched": len(entries),
            "entries_kept": len(articles),
            "error": error,
            "fetched_at": fetched_at,
            "duration_ms": duration_ms,
            "raw_bytes": raw_bytes,
            "raw_hash": hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else None,
            "articles": articles,
            "feed_title": feed_title,
        }
    except requests.exceptions.Timeout:
        error = "timeout"
        return {
            "feed_url": feed_url,
            "status": "error",
            "http_status": status_code,
            "bozo": False,
            "bozo_exception": None,
            "entries_fetched": 0,
            "entries_kept": 0,
            "error": error,
            "fetched_at": fetched_at,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "raw_bytes": raw_bytes,
            "raw_hash": hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else None,
            "articles": [],
            "feed_title": None,
        }
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:200]}"
        return {
            "feed_url": feed_url,
            "status": "error",
            "http_status": status_code,
            "bozo": bozo,
            "bozo_exception": bozo_exc,
            "entries_fetched": 0,
            "entries_kept": 0,
            "error": error,
            "fetched_at": fetched_at,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "raw_bytes": raw_bytes,
            "raw_hash": hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else None,
            "articles": [],
            "feed_title": None,
        }


def fetch_all_feeds(feed_urls: list[str], limit: int = 20, run_dir: Path | None = None) -> list[dict]:
    """Fetch each feed sequentially (ponytail: no ThreadPool for Phase 1, deterministic).

    limit is total desired articles; per-feed we fetch limit//len(feeds) but we
    actually fetch all entries and trim later (feedparser returns ~10-20 anyway).
    If run_dir is given, raw XML is persisted to run_dir/raw/<safe>.xml
    """
    results = []
    for url in feed_urls:
        res = fetch_single_feed(url)
        # persist raw if requested
        if run_dir is not None and res.get("raw_bytes"):
            try:
                raw_dir = Path(run_dir) / "raw"
                raw_dir.mkdir(parents=True, exist_ok=True)
                name = _safe_name(url)
                # keep extension xml
                out = raw_dir / f"{name}.xml"
                out.write_bytes(res["raw_bytes"])
                # also store raw path in result for sources.json
                res["raw_path"] = f"raw/{name}.xml"
            except Exception as e:
                log(f"raw save failed for {url}: {e}")
                res["raw_path"] = None
        else:
            res["raw_path"] = None
        # trim articles per feed to avoid one feed dominating? keep all, trimming done after dedup
        results.append(res)
        # small politeness delay
        time.sleep(0.3)
    return results
