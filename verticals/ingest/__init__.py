"""Ingest package — RSS (+Reddit Phase 2) + normalization + persistence.

Phase 2: RSS primary, Reddit optional community signal (non-blocking).
"""

from .rss import fetch_all_feeds
from .reddit import fetch_reddit_signals
from .store import create_run_dir, save_articles, save_sources, save_raw, save_community

__all__ = ["fetch_all_feeds", "fetch_reddit_signals", "create_run_dir", "save_articles", "save_sources", "save_community", "save_raw"]


def run_ingest(niche: str = "apple", limit: int = 20, runs_dir=None, save_raw_files: bool = True, with_reddit: bool = False, reddit_limit: int = 10):
    """High-level helper for programmatic use (tests, scripts)."""
    from pathlib import Path
    from ..config import RUNS_DIR as DEFAULT_RUNS
    from ..niche import load_niche, get_discovery_config
    from .normalize import dedup_articles

    runs_dir = Path(runs_dir) if runs_dir else DEFAULT_RUNS
    profile = load_niche(niche)
    discovery = get_discovery_config(profile)
    feeds = discovery.get("rss") or []
    if not feeds:
        feeds = ["https://feeds.macrumors.com/MacRumors-All"]

    run_dir = create_run_dir(runs_dir, niche)
    feed_results = fetch_all_feeds(feeds, limit=limit, run_dir=run_dir if save_raw_files else None)
    all_articles = []
    for fr in feed_results:
        all_articles.extend(fr.get("articles", []))
    deduped = dedup_articles(all_articles)
    deduped = sorted(deduped, key=lambda a: a.get("published_at") or "", reverse=True)[:limit]

    save_articles(run_dir, deduped)
    save_sources(run_dir, feed_results, niche=niche, limit=limit)

    # Reddit optional (Phase 2)
    reddit_results = []
    community_signals = []
    if with_reddit:
        reddit_subs = discovery.get("reddit") or []
        # fallback defaults for apple if discovery reddit empty but with_reddit requested
        if not reddit_subs and niche == "apple":
            reddit_subs = ["apple", "iphone"]
        if reddit_subs:
            reddit_results = fetch_reddit_signals(reddit_subs, reddit_limit=reddit_limit, run_dir=run_dir if save_raw_files else None)
            # flatten signals
            for r in reddit_results:
                community_signals.extend(r.get("signals", []))
            # trim to reddit_limit newest
            community_signals = sorted(community_signals, key=lambda s: s.get("published_at") or "", reverse=True)[:reddit_limit]
            save_community(run_dir, reddit_results, community_signals, niche=niche)
        else:
            # no reddit config -> empty community
            save_community(run_dir, [], [], niche=niche)

    return run_dir, deduped, feed_results, community_signals, reddit_results
