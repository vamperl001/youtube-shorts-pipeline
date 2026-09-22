"""Normalization: RSS entry → Article (deterministic, no LLM)."""

import hashlib
import html
import re
import calendar
import time as _time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

# utm and tracking params to strip
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "sr",
}

def canonical_url(url: str) -> str:
    """Strip fragment, tracking query params, trailing slash normalization."""
    if not url:
        return ""
    url = url.strip()
    try:
        parsed = urlparse(url)
        # strip fragment
        fragment = ""
        # filter query
        qsl = parse_qsl(parsed.query, keep_blank_values=True)
        filtered = [(k, v) for k, v in qsl if k.lower() not in _TRACKING_PARAMS]
        query = urlencode(filtered, doseq=True)
        # normalize netloc lower
        netloc = parsed.netloc.lower()
        # path: keep as is but remove trailing slash if >1 char? keep simple: strip one trailing slash
        path = parsed.path
        # reconstruct without fragment
        canon = urlunparse((parsed.scheme.lower(), netloc, path, parsed.params, query, fragment))
        # edge: urlparse keeps // without scheme? ensure scheme
        if not parsed.scheme:
            # assume https if missing
            canon = "https://" + canon.lstrip("/")
        return canon
    except Exception:
        return url.split("#")[0].strip()


def _parse_published(entry: dict) -> str | None:
    """Parse published/updated from feedparser entry to ISO8601 UTC or None."""
    # feedparser sets *.parsed as time.struct_time in UTC
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                # struct is time.struct_time (UTC)
                ts = calendar.timegm(struct)
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                return dt.isoformat().replace("+00:00", "Z")
            except Exception:
                pass
    # fallback: try string fields
    for key in ("published", "updated", "pubDate"):
        val = entry.get(key)
        if val and isinstance(val, str):
            # try feedparser's _parse_date if available, else try dateutil fallback via email.utils
            try:
                import email.utils
                tup = email.utils.parsedate_tz(val)
                if tup:
                    ts = email.utils.mktime_tz(tup)
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    return dt.isoformat().replace("+00:00", "Z")
            except Exception:
                pass
    return None


def _clean_text(s: str, max_len: int = 5000) -> str:
    if not s:
        return ""
    # unescape html, strip tags, collapse whitespace
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len].rstrip() + "…"
    return s


def _source_from_url(feed_url: str) -> str:
    try:
        netloc = urlparse(feed_url).netloc.lower()
        # strip www.
        if netloc.startswith("www."):
            netloc = netloc[4:]
        mapping = {
            "feeds.macrumors.com": "macrumors",
            "9to5mac.com": "9to5mac",
            "appleinsider.com": "appleinsider",
            "cultofmac.com": "cultofmac",
            "selfh.st": "selfhosted",
            "hnrss.org": "hackernews",
        }
        for k, v in mapping.items():
            if k in netloc:
                return v
        # fallback: first label
        return netloc.split(".")[0]
    except Exception:
        return "unknown"


def _article_id(canonical: str) -> str:
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"rss_{h}"


def normalize_entry(entry: dict, feed_url: str, fetched_at: str) -> dict:
    """Single RSS entry → Article dict."""
    # title
    title = _clean_text(str(entry.get("title", "") or "").strip(), max_len=500)
    # url: prefer link, fallback id/guid
    raw_url = entry.get("link") or entry.get("id") or entry.get("guid") or ""
    raw_url = str(raw_url).strip()
    canon = canonical_url(raw_url) if raw_url else ""
    # guid separate
    guid = str(entry.get("id") or entry.get("guid") or canon or title)[:500]
    # summary/content
    summary = ""
    if entry.get("summary"):
        summary = _clean_text(str(entry.get("summary")), max_len=2000)
    elif entry.get("description"):
        summary = _clean_text(str(entry.get("description")), max_len=2000)
    # content may be list
    content = ""
    if entry.get("content"):
        try:
            # feedparser content is list of dicts {value:...}
            vals = entry.get("content")
            if isinstance(vals, list) and vals:
                # take first
                v = vals[0].get("value", "") if isinstance(vals[0], dict) else str(vals[0])
                content = _clean_text(str(v), max_len=8000)
        except Exception:
            content = ""
    # if still empty, summary is content
    if not content:
        content = summary

    published_at = _parse_published(entry)
    source = _source_from_url(feed_url)

    return {
        "article_id": _article_id(canon or guid),
        "source": source,
        "source_type": "rss",
        "url": raw_url,
        "canonical_url": canon,
        "guid": guid,
        "title": title,
        "summary": summary,
        "content": content,
        "published_at": published_at,
        "fetched_at": fetched_at,
        "entities": [],
        "topics": [],
        "source_quality": "primary_news" if source in ("macrumors", "9to5mac", "appleinsider", "cultofmac") else "community",
        "status": "reported",
        "raw": {
            "feed_url": feed_url,
            "rss_guid": guid,
        },
    }


def dedup_articles(articles: list[dict]) -> list[dict]:
    """Dedup by canonical_url (or article_id), keep newest published_at."""
    seen: dict[str, dict] = {}
    for a in articles:
        key = a.get("canonical_url") or a.get("article_id")
        if not key:
            continue
        existing = seen.get(key)
        if not existing:
            seen[key] = a
        else:
            # keep newer published_at
            pa = a.get("published_at") or ""
            pb = existing.get("published_at") or ""
            if pa > pb:
                seen[key] = a
    return list(seen.values())
