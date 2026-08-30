"""Daily pipeline: discover topic → draft → produce → share."""
import sys, json, os, urllib.request, xml.etree.ElementTree as ET
from pathlib import Path

NICHES_DIR = Path(__file__).parent.parent / "niches"
MEDIA_DIR = Path(os.environ.get("VERTICALS_MEDIA", os.path.expanduser("~/.verticals/media")))
EXCHANGE_DIR = Path("/srv/docker/hermes/exchange/Sammlung/MoneyMaker")

def fetch_trending():
    """Scrape Reddit + HN for self-hosting topics."""
    topics = []
    for sub in ["selfhosted", "homelab", "homeserver", "docker", "proxmox"]:
        try:
            url = f"https://www.reddit.com/r/{sub}/hot.rss?limit=8"
            req = urllib.request.Request(url, headers={"User-Agent": "verticals/1.0"})
            xml = urllib.request.urlopen(req, timeout=15).read()
            root = ET.fromstring(xml)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            for e in root.findall("a:entry", ns)[:8]:
                t = (e.find("a:title", ns).text or "").strip()
                if len(t) > 20 and not any(x in t.lower() for x in ["megathread", "quarter", "rules"]):
                    topics.append(f"r/{sub}: {t}")
        except Exception:
            pass
    try:
        ids = json.loads(urllib.request.urlopen(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10).read())[:15]
        for i in ids:
            d = json.loads(urllib.request.urlopen(
                f"https://hacker-news.firebaseio.com/v0/item/{i}.json", timeout=5).read())
            if d and d.get("title") and d.get("score", 0) > 50:
                topics.append(f"HN [{d['score']}↑]: {d['title']}")
    except Exception:
        pass
    return topics

def pick_topic_gemini(topics: list, api_key: str) -> str:
    """Ask Gemini which topic is best for a self-hosting YouTube Shorts channel."""
    import urllib.request as ur
    prompt = (
        "Pick the single most engaging topic for a daily self-hosting YouTube Shorts channel. "
        "Reply with ONLY the topic rephrased as a short sentence (max 15 words). "
        "Focus on: actionable tips, controversy, or trending drama.\n\n"
        "Topics:\n" + "\n".join(f"- {t}" for t in topics[:20])
    )
    data = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 100}
    }).encode()
    req = ur.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/"
            + os.environ.get("GEMINI_LLM_MODEL", "gemini-flash-latest") + ":generateContent",
        data=data,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
    )
    resp = json.loads(ur.urlopen(req, timeout=30).read())
    text = resp["candidates"][0]["content"]["parts"][0]["text"].strip().strip('"').strip("'")
    return text

def run_daily():
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set")
        sys.exit(1)

    print("Fetching trending topics...")
    topics = fetch_trending()
    if not topics:
        print("No topics found, aborting.")
        sys.exit(1)
    print(f"Found {len(topics)} topics")

    print("Picking best topic via Gemini...")
    topic = pick_topic_gemini(topics, api_key)
    print(f"Selected: {topic}")

    # Run full pipeline via CLI
    from .__main__ import main as cli_main
    sys.argv = ["verticals", "produce", "--topic", topic, "--niche", "selfhosting",
                "--lang", "en", "--provider", "gemini", "--force"]
    cli_main()

    # Find latest video and copy to share
    videos = sorted(MEDIA_DIR.glob("verticals_*_en.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if videos:
        latest = videos[0]
        EXCHANGE_DIR.mkdir(parents=True, exist_ok=True)
        dest = EXCHANGE_DIR / f"daily_{latest.stem.split('_')[1]}.mp4"
        import shutil
        shutil.copy2(latest, dest)
        print(f"Shared: {dest}")
    else:
        print("WARNING: no video found after produce")

if __name__ == "__main__":
    run_daily()
