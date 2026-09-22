"""Reddit ingest — optional community signal (non-blocking, 429 tolerant)."""

import hashlib
import time
import re
import html
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import feedparser

from ..log import log


def _safe_name(sub: str) -> str:
    h = hashlib.sha256(sub.encode()).hexdigest()[:6]
    return f"reddit_{sub}_{h}"


def _clean(s: str, max_len: int = 500) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]


def _canonical(url: str) -> str:
    if not url:
        return ""
    return url.split("#")[0].strip()[:1000]


def fetch_single_subreddit(subreddit: str, timeout: int = 12) -> dict:
    """Fetch hot.rss for one subreddit. Returns {subreddit, status, http_status, ... signals}"""
    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    headers = {"User-Agent": "verticals/1.0 (+reddit-signal)"}
    # ponytail: try www, then old.reddit as fallback
    urls = [
        f"https://www.reddit.com/r/{subreddit}/hot.rss?limit=10",
        f"https://old.reddit.com/r/{subreddit}/hot.rss?limit=10",
    ]
    raw_bytes = None
    status_code = None
    error = None
    signals = []
    bozo = False
    chosen_url = urls[0]

    for url in urls:
        chosen_url = url
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            status_code = r.status_code
            raw_bytes = r.content
            if status_code == 429:
                error = "HTTP 429 rate-limited"
                # try next url (old.reddit may not be limited)
                if url == urls[0]:
                    time.sleep(1)
                    continue
                break
            if status_code == 403:
                error = "HTTP 403 blocked"
                if url == urls[0]:
                    continue
                break
            if status_code != 200:
                error = f"HTTP {status_code}"
                break
            # parse
            parsed = feedparser.parse(raw_bytes)
            bozo = bool(getattr(parsed, "bozo", False))
            entries = getattr(parsed, "entries", []) or []
            for e in entries[:10]:
                title = _clean(str(e.get("title", "") or ""), 300)
                link = str(e.get("link", "") or "").strip()
                if not title or not link:
                    continue
                # skip stickied/pinned via title heuristics? keep but mark
                # summary may contain author info
                summary = _clean(str(e.get("summary", "") or ""), 500)
                # published
                published_at = None
                try:
                    import calendar
                    struct = e.get("published_parsed") or e.get("updated_parsed")
                    if struct:
                        ts = calendar.timegm(struct)
                        published_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                except Exception:
                    pass
                canon = _canonical(link)
                sig_id = hashlib.sha256(canon.encode()).hexdigest()[:12]
                signals.append({
                    "signal_id": f"rdt_{sig_id}",
                    "subreddit": subreddit,
                    "title": title,
                    "url": link,
                    "canonical_url": canon,
                    "summary": summary,
                    "published_at": published_at,
                    "fetched_at": fetched_at,
                    "source": f"reddit/r/{subreddit}",
                    "source_type": "reddit",
                    "source_quality": "community",
                    "status": "community",
                    "trending_score": 0.5,
                })
            error = None
            break
        except requests.exceptions.Timeout:
            error = "timeout"
            break
        except Exception as e:
            error = f"{type(e).__name__}: {str(e)[:120]}"
            break

    # status
    if signals:
        status = "ok"
        error = None
    elif error and "429" in error:
        status = "rate_limited"
    elif error:
        status = "error"
    else:
        status = "empty"

    return {
        "subreddit": subreddit,
        "feed_url": chosen_url,
        "status": status,
        "http_status": status_code,
        "bozo": bozo,
        "error": error,
        "fetched_at": fetched_at,
        "duration_ms": 0,  # filled by caller if needed
        "signals_fetched": len(signals),
        "signals": signals,
        "raw_bytes": raw_bytes,
        "raw_hash": hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else None,
    }


def fetch_reddit_signals(subreddits: list[str], reddit_limit: int = 10, run_dir: Path | None = None) -> list[dict]:
    """Fetch multiple subreddits sequentially (ponytail: no threadpool)."""
    results = []
    for sub in subreddits:
        start = time.monotonic()
        res = fetch_single_subreddit(sub)
        res["duration_ms"] = int((time.monotonic() - start) * 1000)
        # trim signals per sub to reddit_limit // len(subreddits) but keep at least 2
        # we keep all then global limit applied later
        if run_dir and res.get("raw_bytes"):
            try:
                raw_dir = Path(run_dir) / "raw"
                raw_dir.mkdir(parents=True, exist_ok=True)
                name = _safe_name(sub)
                out = raw_dir / f"{name}.xml"
                out.write_bytes(res["raw_bytes"])
                res["raw_path"] = f"raw/{name}.xml"
            except Exception as e:
                log(f"reddit raw save failed {sub}: {e}")
                res["raw_path"] = None
        else:
            res["raw_path"] = None
        # remove raw_bytes from result to avoid json bloat (kept only in file)
        res.pop("raw_bytes", None)
        results.append(res)
        # politeness
        time.sleep(0.6)
        # if we already hit rate_limited on first, subsequent may also 429 — continue
    # global limit: flatten and limit
    return results
