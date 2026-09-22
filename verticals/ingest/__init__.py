"""Ingest package — RSS ingestion + normalization + persistence.

Phase 1: RSS only, no LLM, no Apple Newsroom.
Public API: run_ingest(), replay_ingest()
"""

from .rss import fetch_all_feeds
from .store import create_run_dir, save_articles, save_sources, save_raw

__all__ = ["fetch_all_feeds", "create_run_dir", "save_articles", "save_sources", "save_raw"]


def run_ingest(niche: str = "apple", limit: int = 20, runs_dir=None, save_raw_files: bool = True):
    """High-level helper for programmatic use (tests, scripts)."""
    from pathlib import Path
    from ..config import RUNS_DIR as DEFAULT_RUNS
    from ..niche import load_niche, get_discovery_config
    from .normalize import dedup_articles

    runs_dir = Path(runs_dir) if runs_dir else DEFAULT_RUNS
    profile = load_niche(niche)
    discovery = get_discovery_config(profile)
    feeds = discovery.get("rss") or []
    # fallback: if no rss in niche, use general or apple defaults
    if not feeds:
        feeds = ["https://feeds.macrumors.com/MacRumors-All"]

    run_dir = create_run_dir(runs_dir, niche)
    feed_results = fetch_all_feeds(feeds, limit=limit, run_dir=run_dir if save_raw_files else None)
    # flatten and dedup
    all_articles = []
    for fr in feed_results:
        all_articles.extend(fr.get("articles", []))
    deduped = dedup_articles(all_articles)
    # trim to limit after dedup (keep newest first)
    deduped = sorted(deduped, key=lambda a: a.get("published_at") or "", reverse=True)[:limit]

    save_articles(run_dir, deduped)
    save_sources(run_dir, feed_results, niche=niche, limit=limit)
    return run_dir, deduped, feed_results
