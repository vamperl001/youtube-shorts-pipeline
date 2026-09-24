"""Asset Resolver — Phase 6: Apple first, kein Stock-Fallback.

Priorität:
1. apple.com / official Apple (og:image von Produktseite)
2. macrumors/9to5/appleinsider/cultofmac og:image (nur wenn asset_type press_image)
3. missing (kein zufälliges Bild)

Speichert: {shot_idx, story_id, asset_type, preferred_source, status found/missing/synthetic, url, source_domain, http_status, content_type, width, height, ratio, hash, fetched_at, file_path}
"""

import re
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
import html

import requests

from .log import log

# Pillow for dimensions
try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False

ALLOWED_DOMAINS = {"apple.com", "macrumors.com", "9to5mac.com", "appleinsider.com", "cultofmac.com"}


def _apple_product_url(subject: str) -> str:
    """Map subject to Apple product page (deterministisch, kein Hardcode-CDN)."""
    s = subject.lower()
    if "iphone" in s:
        # iPhone 16 Pro ist aktuellste offizielle Seite, 18 noch nicht live -> fallback auf /iphone/
        if "16 pro" in s or "15 pro" in s or "18 pro" in s:
            return "https://www.apple.com/iphone-16-pro/"
        return "https://www.apple.com/iphone/"
    if "mac mini" in s or "macmini" in s:
        return "https://www.apple.com/mac-mini/"
    if "mac studio" in s:
        return "https://www.apple.com/mac-studio/"
    if "macbook" in s:
        return "https://www.apple.com/macbook-air/"
    if "watch" in s:
        return "https://www.apple.com/watch/"
    if "ipad" in s:
        return "https://www.apple.com/ipad-pro/"
    if "airpods" in s:
        return "https://www.apple.com/airpods-max/"
    # generic Apple
    return "https://www.apple.com/"

def _extract_og_image(html_text: str, base_url: str) -> str | None:
    """Extrahiere og:image / twitter:image aus HTML."""
    # suche meta property og:image
    # ponytail: regex statt Parser, minimal
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html_text, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html_text, re.I)
    if m:
        url = html.unescape(m.group(1).strip())
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = urljoin(base_url, url)
        return url
    m = re.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', html_text, re.I)
    if m:
        url = html.unescape(m.group(1).strip())
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = urljoin(base_url, url)
        return url
    return None

def _fetch_og_image(page_url: str, timeout: int = 10) -> tuple[str | None, int | None]:
    """Hole og:image URL von Seite, return (og_url, http_status)."""
    headers = {"User-Agent": "verticals/1.0 (+asset-resolver)"}
    try:
        r = requests.get(page_url, headers=headers, timeout=timeout)
        status = r.status_code
        if status != 200:
            return None, status
        og = _extract_og_image(r.text, page_url)
        return og, status
    except Exception as e:
        log(f"og:image fetch fehlgeschlagen {page_url[:60]}: {e}")
        return None, None

def _looks_like_logo_placeholder(im) -> bool:
    """Erkennt Logo-Platzhalter (z. B. graues Apple-Logo auf Weiß, 24.09. im Video):
    fast nur Weiß + kaum Farbvarianz. Solche og:images (Apple-Fallback wenn die
    Produktseite kein echtes Bild liefert) wirken im Video wie ein Fehler."""
    try:
        small = im.convert("RGB").resize((64, 64))
        px = list(small.getdata())
        white = sum(1 for r, g, b in px if r > 235 and g > 235 and b > 235)
        if white / len(px) < 0.88:
            return False
        import statistics
        gray = [(r + g + b) / 3 for r, g, b in px]
        return statistics.pstdev(gray) < 40
    except Exception:
        return False


def _download_image(img_url: str, dest: Path, timeout: int = 15) -> tuple[bool, str | None, str | None, int | None, int | None, float | None]:
    """Download image, return (ok, content_type, hash, width, height, ratio)."""
    headers = {"User-Agent": "verticals/1.0 (+asset-resolver)", "Accept": "image/*,*/*"}
    try:
        r = requests.get(img_url, headers=headers, timeout=timeout, stream=True)
        if r.status_code != 200:
            return False, None, None, None, None, None
        content_type = r.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            # trotzdem versuchen, wenn url auf .jpg/.png endet
            if not any(img_url.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
                return False, content_type, None, None, None, None
        data = r.content
        if len(data) < 5_000:
            return False, content_type, None, None, None, None
        # hash
        h = hashlib.sha256(data).hexdigest()[:16]
        # dimensions
        w = h2 = None
        ratio = None
        if HAS_PIL:
            try:
                from io import BytesIO
                im = Image.open(BytesIO(data))
                w, h2 = im.size
                ratio = round(w / h2, 2) if h2 else None
                # Mindestgröße: Thumbnails/Logos (<40KB oder <500px) ablehnen
                if len(data) < 40_000 or (w and h2 and min(w, h2) < 500):
                    log(f"Bild zu klein/Thumbnail, übersprungen ({img_url[:60]}, {len(data)//1024}KB {w}x{h2})")
                    return False, content_type, None, None, None, None
                if _looks_like_logo_placeholder(im):
                    log(f"Logo-Platzhalter erkannt, übersprungen ({img_url[:60]})")
                    return False, content_type, None, None, None, None
            except Exception:
                pass
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True, content_type, h, w, h2, ratio
    except Exception as e:
        log(f"download fehlgeschlagen {img_url[:60]}: {e}")
        return False, None, None, None, None, None

def _candidate_urls_for_shot(shot: dict, editorial: dict, articles: list[dict]) -> list[tuple[str, str]]:
    """Generiere Kandidaten-URLs in Prioritätsreihenfolge: apple.com -> story sources -> preferred."""
    story_id = shot.get("story_id")
    preferred = shot.get("preferred_source", "apple.com")
    asset_type = shot.get("asset_type", "")
    subject = shot.get("subject", "")

    candidates: list[tuple[str, str]] = []  # (url, domain)

    # title_card: keine externe URL
    if asset_type == "title_card":
        return []

    # 1. apple.com Produktseite (für alle Shots, da Apple-first)
    if subject:
        apple_url = _apple_product_url(subject)
        candidates.append((apple_url, "apple.com"))

    # 2. Story-Quellen (aus editorial)
    ed_map = {s.get("story_id"): s for s in editorial.get("stories", [])}
    ed = ed_map.get(story_id)
    if ed:
        amap = {a.get("article_id"): a for a in articles}
        for aid in ed.get("sources", []):
            art = amap.get(aid)
            if art and art.get("url"):
                url = art["url"]
                domain = urlparse(url).netloc.lower().replace("www.", "")
                # nur erlaubte Domains
                if any(d in domain for d in ALLOWED_DOMAINS):
                    candidates.append((url, domain))

    # 3. Preferred source Fallback (wenn nicht schon dabei)
    # Für apple.com bereits oben, sonst versuche generische Seite
    if preferred not in [d for _, d in candidates]:
        if preferred == "apple.com" and subject:
            # schon oben
            pass
        elif preferred in ALLOWED_DOMAINS:
            # für nicht-apple: versuche die Domain's Startseite als Fallback? ponytail: nicht, nur wenn story keine URL hat
            # wir versuchen https://preferred/
            candidates.append((f"https://{preferred}/", preferred))

    # Deduplizieren
    seen = set()
    deduped = []
    for url, dom in candidates:
        key = url.split("#")[0]
        if key not in seen:
            seen.add(key)
            deduped.append((url, dom))
    return deduped[:5]

def resolve_assets(shots_data: dict, editorial: dict, articles: list[dict], run_dir: Path, timeout: int = 10) -> list[dict]:
    """Hauptfunktion: löse alle Shots auf, speichere assets/*.jpg, return assets list."""
    run_dir = Path(run_dir)
    assets_dir = run_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    shots = shots_data.get("shots", [])
    assets = []
    story_pos: dict[str, int] = {}
    for shot in shots:
        idx = shot.get("idx")
        story_id = shot.get("story_id")
        asset_type = shot.get("asset_type")
        preferred = shot.get("preferred_source")
        subject = shot.get("subject", "")
        duration = shot.get("duration", 3)

        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        asset: dict = {
            "shot_idx": idx,
            "story_id": story_id,
            "subject": subject,
            "asset_type": asset_type,
            "preferred_source": preferred,
            "duration": duration,
            "status": "missing",
            "url": None,
            "source_domain": None,
            "http_status": None,
            "content_type": None,
            "width": None,
            "height": None,
            "ratio": None,
            "hash": None,
            "fetched_at": fetched_at,
            "file_path": None,
            "candidate_urls": [],
        }

        # title_card -> synthetic
        if asset_type == "title_card":
            asset["status"] = "synthetic"
            asset["source_domain"] = "synthetic"
            assets.append(asset)
            log(f"Asset {idx} [{story_id}] title_card -> synthetic")
            continue

        candidates = _candidate_urls_for_shot(shot, editorial, articles)
        asset["candidate_urls"] = [u for u, _ in candidates]
        # Rotation pro Story-Position: Shots derselben Story starten bei
        # unterschiedlichen Kandidaten (sonst landet 2x dasselbe og:image).
        # Erster Shot jeder Story behält die Best-Reihenfolge (apple.com zuerst).
        pos = story_pos.get(story_id or "", 0)
        story_pos[story_id or ""] = pos + 1
        if len(candidates) > 1 and pos > 0:
            rot = pos % len(candidates)
            candidates = candidates[rot:] + candidates[:rot]
        found = False
        for cand_url, cand_domain in candidates:
            # für press_image: nur wenn candidate domain zu asset_type passt? ponytail: alle erlaubten Domains ok
            # Hole og:image
            og_url, status = _fetch_og_image(cand_url, timeout=timeout)
            asset["http_status"] = status
            if not og_url:
                log(f"Asset {idx} [{story_id}] {cand_domain} kein og:image ({cand_url[:50]})")
                continue
            # Download og:image
            dest = assets_dir / f"shot_{idx:02d}.jpg"
            ok, ctype, h, w, h2, ratio = _download_image(og_url, dest, timeout=timeout)
            if ok:
                asset.update({
                    "status": "found",
                    "url": og_url,
                    "source_domain": cand_domain,
                    "http_status": 200,
                    "content_type": ctype,
                    "width": w,
                    "height": h2,
                    "ratio": ratio,
                    "hash": h,
                    "file_path": f"assets/shot_{idx:02d}.jpg",
                })
                log(f"Asset {idx} [{story_id}] found {cand_domain} {og_url[:50]} {w}x{h2}")
                found = True
                break
            else:
                log(f"Asset {idx} [{story_id}] og:image download failed {og_url[:50]}")
                continue
        if not found and asset["status"] != "found":
            asset["status"] = "missing"
            log(f"Asset {idx} [{story_id}] missing (alle Kandidaten fehlgeschlagen)")
        assets.append(asset)
        # politeness
        time.sleep(0.4)

    return assets

def save_assets(run_dir: Path, assets: list[dict]):
    """Speichere assets.json."""
    import json
    run_dir = Path(run_dir)
    out = run_dir / "assets.json"
    # sort by idx
    assets_sorted = sorted(assets, key=lambda a: a.get("shot_idx", 0))
    out.write_text(json.dumps(assets_sorted, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Saved assets → {out} ({len([a for a in assets if a['status']=='found'])}/{len(assets)} found)")
