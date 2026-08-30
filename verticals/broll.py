"""Gemini Imagen b-roll generation + Ken Burns animation."""

import base64
from pathlib import Path

import requests
from PIL import Image

from .config import VIDEO_WIDTH, VIDEO_HEIGHT, get_gemini_key, get_gemini_image_model, run_cmd
from .log import log
from .retry import with_retry


@with_retry(max_retries=3, base_delay=2.0)
def _generate_image_gemini(prompt: str, output_path: Path, api_key: str):
    """Generate image via Gemini native image generation (free tier compatible)."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta"
        "/models/" + get_gemini_image_model() + ":generateContent"
    )
    body = {
        "contents": [{"parts": [{"text": f"Generate an image: {prompt}"}]}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }
    r = requests.post(
        url, json=body, timeout=90,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    if r.status_code != 200:
        try:
            detail = r.json().get("error", {}).get("message", r.text[:200])
        except Exception:
            detail = r.text[:200]
        hint = ""
        if r.status_code == 403:
            hint = (
                " — check that GEMINI_API_KEY is set in this environment and is "
                "an AI Studio key (https://aistudio.google.com/apikey), not a "
                "Vertex AI / service-account credential"
            )
        raise RuntimeError(f"Gemini API {r.status_code}: {detail}{hint}")
    data = r.json()
    # Extract image from response parts
    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            img_b64 = part["inlineData"]["data"]
            output_path.write_bytes(base64.b64decode(img_b64))
            return
    raise RuntimeError("No image in Gemini response")


def _fetch_stock_pexels(prompt: str, output_path: Path) -> Path | None:
    """Pexels Stock-Foto: echte Fotos statt KI-Generation. Portrait 9:16 bevorzugt."""
    import os, time as _time, urllib.parse
    api_key = os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        return None
    # Prompt auf Kernelemente kürzen (Pexels mag 2-5 Schlagworte)
    query = " ".join(prompt.split()[:8])
    url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode({
        "query": query, "per_page": 5, "orientation": "portrait"
    })
    try:
        req = urllib.request.Request(url, headers={"Authorization": api_key})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        photos = data.get("photos", [])
        if not photos:
            log(f"Pexels: keine Treffer für '{query}'")
            return None
        # Bestes Foto: größte Höhe (portrait-orientiert)
        photos.sort(key=lambda p: p.get("height", 0), reverse=True)
        img_url = photos[0]["src"]["large2x"]
        req2 = urllib.request.Request(img_url, headers={"User-Agent": "verticals/3.1"})
        with urllib.request.urlopen(req2, timeout=20) as resp2:
            img_data = resp2.read()
        if len(img_data) < 10_000:
            return None
        output_path.write_bytes(img_data)
        log(f"Pexels: Stock-Foto geladen ({len(img_data)//1024} KB)")
        return output_path
    except Exception as e:
        log(f"Pexels-Fehler: {e}")
        return None

def fetch_stock_images(query: str, n: int, out_dir: Path, start: int,
                       keywords: list[str] | None = None) -> list[Path]:
    """Keyless CC-Fotos von Openverse — mehrere Suchbegriffe für Vielfalt."""
    import re
    import json as _json
    import urllib.parse

    # Build diverse queries from keywords — rotiere basierend auf start-Offset
    kw = [w.lower() for w in (keywords or []) if len(w) > 2]
    queries = []
    if kw:
        # Rotiere Keywords basierend auf start (Frame-Index) für Vielfalt
        offset = start % max(len(kw), 1)
        rotated = kw[offset:] + kw[:offset]
        # Single words
        for w in rotated[:6]:
            queries.append(w)
        # One pair for specificity
        if len(kw) >= 2:
            queries.append(f"{kw[0]} {kw[1]}")
    else:
        stop = {"with","and","for","the","your","this","that","from","into"}
        words = [w for w in re.findall(r"[A-Za-z]+", query)
                 if w.lower() not in stop][:3] or ["server"]
        queries = words[:3]

    log(f"Stock-Suche: {queries[:6]}")
    got = []
    seen_ids = set()

    for q_text in queries[:6]:
        if len(got) >= n:
            break
        q = urllib.parse.quote(q_text)
        url = f"https://api.openverse.org/v1/images/?q={q}&page_size={n + 6}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "verticals-homelab/1.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = _json.loads(resp.read())
            for res in data.get("results", []):
                if len(got) >= n:
                    break
                res_id = res.get("id", "")
                if res_id in seen_ids:
                    continue
                seen_ids.add(res_id)
                # Relaxiert: Bild ok wenn URL vorhanden (Openverse = CC-Index)
                u = res.get("url") or res.get("thumbnail")
                if not u:
                    continue
                dst = out_dir / f"stock_{start+len(got)}.jpg"
                try:
                    r2 = urllib.request.Request(u, headers={"User-Agent": "verticals-homelab/1.0"})
                    with urllib.request.urlopen(r2, timeout=30) as r:
                        raw = r.read()
                    if len(raw) > 15_000:
                        dst.write_bytes(raw)
                        got.append(dst)
                except Exception:
                    continue
        except Exception:
            continue
    return got


def _generate_image_pollinations(prompt: str, output_path: Path) -> Path | None:
    """Keyless Gratis-Fallback: pollinations.ai (Portrait 9:16)."""
    import time as _time
    import urllib.parse

    url = (
        "https://image.pollinations.ai/prompt/"
        + urllib.parse.quote(prompt[:400])
        + f"?width=1080&height=1920&nologo=true&seed={int(_time.time())}"
    )
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "verticals/3.1"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = resp.read()
            if len(data) > 20_000 and resp.headers.get("Content-Type", "").startswith("image"):
                output_path.write_bytes(data)
                return output_path
        except Exception:
            pass
        _time.sleep(3)
    return None

def _fallback_frame(i: int, out_dir: Path) -> Path:
    """Solid colour fallback frame if Gemini fails."""
    colors = [(20, 20, 60), (40, 10, 40), (10, 30, 50)]
    img = Image.new("RGB", (VIDEO_WIDTH, VIDEO_HEIGHT), colors[i % len(colors)])
    path = out_dir / f"broll_{i}.png"
    img.save(path)
    return path


_GEMINI_DEAD = False


def _gemini_quota_dead(api_key: str) -> bool:
    global _GEMINI_DEAD
    if _GEMINI_DEAD:
        return True
    try:
        req = urllib.request.Request(
            "https://generativelanguage.googleapis.com/v1beta"
            "/models/" + get_gemini_image_model() + ":generateContent",
            data=json.dumps({"contents": [{"parts": [{"text": "test"}]}]}).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        )
        urllib.request.urlopen(req, timeout=20)
        return False
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        if e.code == 429 and '"limit": 0' in body.replace(" ", ""):
            _GEMINI_DEAD = True
            return True
        return False
    except Exception:
        return False


def generate_broll(prompts: list, out_dir: Path, target_frames: int | None = None,
                   script_text: str = "") -> list[Path]:
    """B-Roll-Frames: Stock (Openverse) -> Gemini -> Pollinations -> Solid."""
    api_key = get_gemini_key()
    prompts = [p for p in prompts if p][:6] or ["server room dark"]
    frames = []
    if target_frames:
        per = max(1, round(target_frames / len(prompts)))
        stock_n = max(target_frames - len(frames), 0)
        idx = 0
        import re as _re
        stop = {"with","and","for","the","your","this","that","from","into","on","in","at",
                "here","are","just","also","still","way","one","two","three","four","five",
                "first","second","third","fourth","fifth","well","than","its","has","had",
                "was","are","not","but","can","may","will","just","like","get","got",
                "make","new","old","big","small","top","use","see","set","run","let",
                "some","any","all","each","every","more","most","less","few","own",
                "put","take","come","go","look","want","give","keep","seem","tell",
                "need","try","ask","turn","start","show","work","call","move","live",
                "feel","leave","bring","happen","must","really","already","back",
                "even","still","also","right","thing","things","people","time",
                "day","way","lot","think","know","see","come","take","want","look",
                "use","find","give","tell","say","said","went","done","dropped",
                "someone","else","someone","basement","running","stabilize","revenue",
                "dropping","platforms","swallow","fees","tips","wages","restaurant"}
        kw = [w for w in _re.findall(r"[A-Za-z]{3,}", script_text)
              if w.lower() not in stop][:8] or ["server","hosting"]
        log(f"Script-Keywords: {kw}")
        for p in prompts:
            got = fetch_stock_images(p, per, out_dir, idx, keywords=kw)
            frames += got
            idx += len(got)
        log(f"Stock-Frames: {len(frames)}/{target_frames}")
        if len(frames) >= target_frames:
            return frames[:target_frames]
    for i, prompt in enumerate(prompts[:3]):
        out_path = out_dir / f"broll_{i}.png"
        # 1) Pexels Stock-Foto (echt, schnell, kostenlos)
        stock = _fetch_stock_pexels(prompt, out_dir / f"broll_{i}_stock.jpg")
        if stock:
            img = Image.open(stock).convert("RGB")
            target_w, target_h = VIDEO_WIDTH, VIDEO_HEIGHT
            scale = max(target_w / img.width, target_h / img.height)
            new_w, new_h = int(img.width * scale), int(img.height * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - target_w) // 2
            top = (new_h - target_h) // 2
            img.crop((left, top, left + target_w, top + target_h)).save(out_path)
            frames.append(out_path)
            continue
        # 2) Gemini Imagen (wenn Key + Quota)
        if api_key:
            log(f"Generating b-roll frame {i+1}/3 via Gemini Imagen...")
            try:
                _generate_image_gemini(prompt, out_path, api_key)
                img = Image.open(out_path).convert("RGB")
                target_w, target_h = VIDEO_WIDTH, VIDEO_HEIGHT
                orig_w, orig_h = img.size
                scale = max(target_w / orig_w, target_h / orig_h)
                new_w, new_h = int(orig_w * scale), int(orig_h * scale)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                left = (new_w - target_w) // 2
                top = (new_h - target_h) // 2
                img = img.crop((left, top, left + target_w, top + target_h))
                img.save(out_path)
                frames.append(out_path)
                continue
            except Exception as e:
                log(f"Frame {i+1} failed: {e} — trying pollinations…")
        else:
            log(f"GEMINI_API_KEY fehlt/ohne Bild-Quota — Frame {i+1}/3 via pollinations…")
        img = _generate_image_pollinations(prompt, out_dir / f"broll_{i+1}.jpg")
        frames.append(img or _fallback_frame(i, out_dir))
    return frames


def animate_frame(img_path: Path, out_path: Path, duration: float, effect: str = "zoom_in"):
    """Ken Burns animation on a single frame."""
    from PIL import ImageOps
    img = Image.open(img_path)
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(img_path)  # corrected orientation for ffmpeg
    fps = 30
    frames = int(duration * fps)
    w, h = VIDEO_WIDTH, VIDEO_HEIGHT
    big = 2  # Supersampling gegen zoompan-Jitter
    sw, sh = w * big, h * big
    d = frames  # shorthand

    if effect == "zoom_in":
        # Subtiler Zoom in Produkt-Mitte (12% range, sanft)
        zp = f"zoompan=z='1.12-0.12*on/{d}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    elif effect == "pan_down":
        # Sanfter Vertikal-Scroll (10% Zoom, langsamer)
        zp = f"zoompan=z=1.15:x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*on/{d}'"
    elif effect == "pan_up":
        # Sanfter Scroll rauf
        zp = f"zoompan=z=1.15:x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*(1-on/{d})'"
    elif effect == "zoom_out":
        # Scale-Reveal: startet leicht gezoomt, zoomt raus (Produkt wird freigelegt)
        zp = f"zoompan=z='1.15-0.15*on/{d}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    elif effect == "scale_reveal":
        # Produkt-Reveal: startet weit, zoomt langsam ins Produkt (15%)
        zp = f"zoompan=z='1.0+0.15*on/{d}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    elif effect == "drift":
        # Subtiles Schweben (5% Zoom + langsamer Drift)
        zp = f"zoompan=z='1.08-0.05*on/{d}':x='iw/2-(iw/zoom/2)+sin(on/{d}*0.8)*20':y='ih/2-(ih/zoom/2)+cos(on/{d}*0.6)*15'"
    else:
        # Fallback: leichtes Driften
        zp = f"zoompan=z='1.08-0.05*on/{d}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"

    vf = (
        f"scale='max({sw},iw*{sh}/ih)':'max({sh},ih*{sw}/iw)',crop={sw}:{sh},"
        f"{zp}:d={frames}:s={int(w*2)}x{int(h*2)}:fps={fps},"
        f"scale={w}:{h}:flags=lanczos,format=yuv420p"
    )

    run_cmd([
        "ffmpeg", "-loop", "1", "-i", str(img_path),
        "-vf", vf, "-t", str(duration), "-r", str(fps),
        str(out_path), "-y", "-loglevel", "error",
    ])
