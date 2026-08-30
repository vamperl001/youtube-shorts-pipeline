"""Source-aware image fetcher — Website-Screenshots, Reddit, HN, PressKit, Charts."""

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .log import log


def fetch_hn_discussion(query: str) -> dict | None:
    """Search Hacker News (Algolia API) for the matching story.

    Returns {'story_url', 'discussion_url', 'title', 'points'} or None.
    """
    stop = {"the","a","an","is","are","of","to","for","and","or","in","on","with",
            "just","announced","released","new","best","vs","now","gets"}
    kws = [w for w in re.findall(r"[a-zA-Z0-9.]+", query.lower()) if w not in stop]

    def _search(terms: list[str], min_points: int) -> dict | None:
        q = urllib.parse.quote_plus(" ".join(terms))
        url = f"https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage=5"
        req = urllib.request.Request(url, headers={"User-Agent": "verticals/1.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        hits = [h for h in data.get("hits", []) if (h.get("points") or 0) >= min_points]
        if not hits:
            return None
        h = max(hits, key=lambda x: x.get("points") or 0)
        return {
            "story_url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
            "discussion_url": f"https://news.ycombinator.com/item?id={h['objectID']}",
            "title": h.get("title", ""),
            "points": h.get("points", 0),
            "comments": h.get("num_comments", 0),
        }

    # Progressiv: alle Keywords -> erste 3 -> erstes Keyword
    for terms, min_pts in [(kws[:6], 10), (kws[:3], 5), (kws[:1], 3)]:
        if not terms:
            continue
        try:
            r = _search(terms, min_pts)
        except Exception as e:
            log(f"HN-Suche fehlgeschlagen ({' '.join(terms)}): {e}")
            return None
        if r:
            return r
    return None


def _chromium_available() -> bool:
    """Check if chromium/chrome is available for screenshots."""
    import shutil
    return shutil.which("chromium") or shutil.which("google-chrome") or shutil.which("chromium-browser")


_CHROMIUM_IMG = "gcr.io/zenika-hub/alpine-chrome:latest"


def _container_screenshot(url: str, out_path: Path, width: int, height: int) -> bool:
    """Echter Screenshot via alpinen Chromium-Container (host-mounted tmp)."""
    import uuid
    import tempfile
    tmp_dir = Path(tempfile.gettempdir()) / f"vcshot_{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    in_tmp = tmp_dir / "shot.png"
    cmd = [
        "docker", "run", "--rm", "--entrypoint", "",
        "-v", f"{tmp_dir}:/out",
        _CHROMIUM_IMG,
        "/usr/bin/chromium", "--headless", "--no-sandbox", "--disable-gpu",
        "--hide-scrollbars",
        f"--window-size={width},{height}",
        "--timeout=45000",
        f"--screenshot=/out/shot.png",
        url,
    ]
    try:
        r = subprocess.run(cmd, timeout=90, check=False, capture_output=True)
        if r.returncode == 0 and in_tmp.exists() and in_tmp.stat().st_size > 5_000:
            shutil.move(in_tmp, out_path)
            return True
        log(f"Container-Screenshot leer/fehlgeschlagen ({url[:40]}) — Fallback thum.io")
    except Exception as e:
        log(f"Container-Screenshot fehlgeschlagen ({url[:40]}): {e} — Fallback thum.io")
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return False


def screenshot_website(url: str, out_path: Path, width: int = 1200, height: int = 2000) -> bool:
    """Echter Screenshot via Chromium-Container, Fallback: Headless-Chromium, thum.io."""

    if _container_screenshot(url, out_path, width, height):
        return True

    browser = _chromium_available()
    if browser:
        cmd = [
            browser, "--headless", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", "--force-device-scale-factor=1",
            f"--window-size={width},{height}",
            "--timeout=45000",
            "--screenshot", str(out_path),
            url,
        ]
        try:
            r = subprocess.run(cmd, timeout=60, check=False, capture_output=True)
            if r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 5_000:
                log(f"Screenshot OK ({url[:40]})")
                return True
            log(f"Chromium-Screenshot leer/fehlgeschlagen ({url[:40]}) — Fallback thum.io")
        except Exception as e:
            log(f"Chromium-Screenshot fehlgeschlagen ({url[:40]}): {e} — Fallback thum.io")
    else:
        log("Kein Chromium verfügbar — Fallback thum.io")

    api = f"https://image.thum.io/get/width/{width}/crop/{height}/{url}"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "verticals/1.0"})
        data = urllib.request.urlopen(req, timeout=30).read()
        if len(data) > 5_000:
            out_path.write_bytes(data)
            return True
    except Exception as e:
        log(f"Screenshot (thum.io) fehlgeschlagen ({url[:40]}): {e}")
    return False


_REDDIT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def _reddit_direct_url(img_url: str) -> str:
    """preview.redd.it hotlinks 403 aus Rechenzentren — auf i.redd.it direkte Datei zeigen."""
    u = img_url.split("?")[0]
    if "preview.redd.it/" in u:
        u = u.replace("preview.redd.it", "i.redd.it")
    return u


def fetch_reddit_images(subreddit: str, limit: int = 10) -> list[dict]:
    """Fetch image URLs from Reddit RSS (top posts with images)."""
    items = []
    candidates = [
        f"https://www.reddit.com/r/{subreddit}/hot.rss?limit={limit}",
        f"https://old.reddit.com/r/{subreddit}/hot.rss?limit={limit}",
    ]
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _REDDIT_UA})
            xml = urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:
            log(f"Reddit RSS Fehler ({url[:50]}): {e}")
            continue
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml)
        except Exception:
            continue
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("a:entry", ns)[:limit]:
            title = entry.find("a:title", ns).text or ""
            content = entry.find("a:content", ns)
            if content is not None and content.text:
                imgs = re.findall(r'src="(https?://[^"]+\.(?:jpg|png|webp))"', content.text)
                for img_url in imgs[:2]:
                    items.append({
                        "title": title,
                        "url": _reddit_direct_url(img_url),
                        "source": f"r/{subreddit}",
                    })
        if items:
            break
    return items


def fetch_presskit_images(domain: str, keywords: list[str]) -> list[str]:
    """Fetch product images from known PressKit CDN URLs."""
    presskits = {
        "apple.com": [
            # Mac Mini (current gen)
            "https://store.storeimages.cdn-apple.com/4982/as-images.apple.com/is/mac-mini-hero-select-202411?wid=1200&hei=900",
            "https://www.apple.com/v/mac-mini/a/images/overview/hero/hero__bqxj2v61czqe_large.jpg",
            # Mac Studio
            "https://store.storeimages.cdn-apple.com/4982/as-images.apple.com/is/mac-studio-select-202406?wid=1200&hei=900",
            "https://www.apple.com/v/mac-studio/a/images/overview/hero/hero__c5q3qz6rmzxu_large.jpg",
            # General Apple silicon
            "https://store.storeimages.cdn-apple.com/4982/as-images.apple.com/is/MXM63?wid=1200&hei=900",
        ],
    }
    urls = []
    for d, u_list in presskits.items():
        if d in domain:
            urls.extend(u_list)
    return urls[:8]


def download_image(url: str, out_path: Path, timeout: int = 20) -> bool:
    """Download a single image. Logs HTTP errors (403/404) instead of silent-skip."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "image/*,*/*",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if len(data) > 10_000:
            out_path.write_bytes(data)
            return True
        log(f"download_image: zu klein/leer ({url[:80]})")
    except urllib.error.HTTPError as e:
        log(f"download_image: HTTP {e.code} bei {url[:80]} ({e.reason})")
    except Exception as e:
        log(f"download_image: {type(e).__name__} bei {url[:80]}: {e}")
    return False


def generate_chart(labels: list[str], values: list[float], title: str, out_path: Path) -> bool:
    """Generate a simple bar chart using matplotlib."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(10.8, 19.2), dpi=100)  # 9:16
        bars = ax.barh(labels, values, color=["#1d1d1f", "#86868b", "#0071e3", "#34c759", "#ff9500"])
        ax.set_title(title, fontsize=24, color="#1d1d1f", pad=20)
        ax.tick_params(axis="y", labelsize=14)
        ax.tick_params(axis="x", labelsize=12)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.patch.set_facecolor("white")
        plt.tight_layout()
        fig.savefig(str(out_path), dpi=100, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return out_path.exists()
    except ImportError:
        log("WARN: matplotlib nicht installiert — kein Chart")
        return False
    except Exception as e:
        log(f"Chart-Fehler: {e}")
        return False


def fetch_source_images(
    topic: str,
    sources: list[dict],
    out_dir: Path,
    n: int = 8,
) -> list[Path]:
    """Fetch images from multiple sources. Each source dict has type + url/params."""
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    idx = 0

    for src in sources[:n]:
        if len(frames) >= n:
            break
        src_type = src.get("type", "")

        if src_type == "screenshot" and src.get("url"):
            dst = out_dir / f"source_{idx}.jpg"
            if screenshot_website(src["url"], dst):
                frames.append(dst)
                idx += 1

        elif src_type == "presskit":
            urls = fetch_presskit_images(src.get("domain", ""), src.get("keywords", []))
            for u in urls:
                if len(frames) >= n:
                    break
                dst = out_dir / f"source_{idx}.jpg"
                if download_image(u, dst):
                    frames.append(dst)
                    idx += 1

        elif src_type == "reddit":
            # Reddit blockt Server-Downloads (403/429) — Screenshot des Subreddits
            # (old.reddit) ist die zuverlässige Quelle statt i.redd.it-Hotlinks.
            sub = src.get("subreddit", "selfhosted")
            dst = out_dir / f"source_{idx}.jpg"
            if screenshot_website(f"https://old.reddit.com/r/{sub}/", dst):
                frames.append(dst)
                idx += 1
            items = fetch_reddit_images(sub, limit=8)
            for item in items:
                if len(frames) >= n:
                    break
                dst = out_dir / f"source_{idx}.jpg"
                if download_image(item["url"], dst):
                    frames.append(dst)
                    idx += 1

        elif src_type == "chart":
            labels = src.get("labels", [])
            values = src.get("values", [])
            title = src.get("title", topic)
            dst = out_dir / f"chart_{idx}.png"
            if labels and generate_chart(labels, values, title, dst):
                frames.append(dst)
                idx += 1

    log(f"Source-Images: {len(frames)}/{n} von {len(sources)} Quellen")
    return frames


def sources_for_topic(topic: str, topic_url: str = "") -> list[dict]:
    """Auto-detect relevant sources: Tool-Website, HN-Diskussion, Reddit-Thread."""
    t = topic.lower()
    sources = []

    # Original-Quelle des Topics zuerst (Reddit-Thread oder Artikel)
    if topic_url:
        if "reddit.com" in topic_url:
            # Thread selbst screenshoten statt Bilder runterzuladen (429-sicher)
            old_url = topic_url.replace("www.reddit.com", "old.reddit.com")
            sources.append({"type": "screenshot", "url": old_url, "label": "reddit_thread"})
        elif topic_url.startswith("http"):
            sources.append({"type": "screenshot", "url": topic_url, "label": "source_article"})

    # HN-Diskussion immer versuchen (Algolia API, kein Rate-Limit)
    hn = fetch_hn_discussion(topic)
    if hn:
        log(f"HN gefunden: {hn['points']} Punkte — {hn['title'][:60]}")
        sources.append({"type": "screenshot", "url": hn["story_url"], "label": "hn_story"})
        sources.append({"type": "screenshot", "url": hn["discussion_url"], "label": "hn_discussion"})
    else:
        log("Kein HN-Treffer — nur Website + Reddit")

    # Apple topics
    if any(w in t for w in ["mac", "apple", "iphone", "ipad", "macbook", "mac studio", "mac mini"]):
        sources.append({"type": "presskit", "domain": "apple.com", "keywords": t.split()})
        sources.append({"type": "screenshot", "url": "https://www.apple.com/mac-mini/"})
        sources.append({"type": "screenshot", "url": "https://www.apple.com/mac-studio/"})

    # GitHub / open source
    if any(w in t for w in ["github", "open source", "foss", "git"]):
        sources.append({"type": "screenshot", "url": "https://github.com/trending"})

    # Self-hosting tools
    tool_urls = {
        "proxmox": "https://www.proxmox.com/en/",
        "docker": "https://www.docker.com/",
        "kubernetes": "https://kubernetes.io/",
        "pi-hole": "https://pi-hole.net/",
        "nextcloud": "https://nextcloud.com/",
        "home assistant": "https://www.home-assistant.io/",
        "grafana": "https://grafana.com/",
        "tailscale": "https://tailscale.com/",
        "jellyfin": "https://jellyfin.org/",
        "vaultwarden": "https://vaultwarden.com/",
        "paperless": "https://github.com/paperless-ngx/paperless-ngx",
        "n8n": "https://n8n.io/",
        "ollama": "https://ollama.com/",
        "immich": "https://immich.app/",
        "adguard": "https://adguard.com/",
        "wireguard": "https://www.wireguard.com/",
        "truenas": "https://www.truenas.com/",
        "unraid": "https://unraid.net/",
        "synology": "https://www.synology.com/",
        "portainer": "https://www.portainer.io/",
        "gitea": "https://gitea.io/",
        "forgejo": "https://forgejo.org/",
        "minio": "https://min.io/",
        "authentik": "https://goauthentik.io/",
        "uptime kuma": "https://uptime.kuma.pet/",
    }
    for tool, url in tool_urls.items():
        if tool in t:
            sources.append({"type": "screenshot", "url": url})

    # Fallback: generic self-hosting
    if not sources:
        sources.append({"type": "reddit", "subreddit": "selfhosted"})

    # Doppelte Screenshot-URLs entfernen (z.B. Artikel == HN-Story)
    seen: set[str] = set()
    dedup = []
    for s in sources:
        if s.get("type") == "screenshot" and s.get("url"):
            u = s["url"].split("#")[0].rstrip("/")
            if u in seen:
                continue
            seen.add(u)
        dedup.append(s)

    return dedup
