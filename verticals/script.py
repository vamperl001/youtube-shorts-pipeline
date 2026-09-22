"""Script Layer — spoken briefing from editorial stories.

LLM formuliert, Code validiert/speichert. Kein Asset/FFmpeg-Bezug.

Input:  editorial.json + articles.json (+ niche)
Output: {edition, intro, stories:[{story_id, headline, text, duration_target, sources, status}], outro, full_script, word_count}
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .log import log
from .llm import call_llm
from .niche import load_niche


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1]
        if raw.lstrip().startswith("json"):
            raw = raw.lstrip()[4:]
        raw = raw.strip()
    return raw

def _repair_json(raw: str) -> str:
    s = raw.strip()
    start = s.find("{")
    if start > 0:
        s = s[start:]
    in_str = False
    esc = False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
    if in_str:
        s += '"'
    depth_brace = 0
    depth_bracket = 0
    in_str = False
    esc = False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket -= 1
    s += "]" * max(depth_bracket, 0)
    s += "}" * max(depth_brace, 0)
    return s

def _parse_script_json(raw: str) -> dict:
    raw = _strip_fences(raw)
    start = raw.find("{")
    end = raw.rfind("}") + 1
    candidate = raw[start:end] if start >= 0 and end > start else raw
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        repaired = _repair_json(candidate)
        return json.loads(repaired)

def _build_story_context(editorial: dict, articles: list[dict]) -> str:
    # map article_id -> article for quick lookup
    amap = {a.get("article_id"): a for a in articles}
    lines = []
    for s in editorial.get("stories", []):
        sid = s.get("story_id", "")
        headline = s.get("headline", "")
        status = s.get("status", "")
        importance = s.get("importance", "")
        reason = s.get("reason", "")
        sources = s.get("sources", [])
        # gather source summaries
        src_texts = []
        for aid in sources:
            art = amap.get(aid)
            if art:
                title = art.get("title", "")[:120]
                summary = art.get("summary", "")[:200]
                src_texts.append(f"[{aid} {art.get('source','')} {art.get('published_at','')[:10]}] {title} — {summary}")
        src_block = "\n".join(src_texts) if src_texts else "(keine Summary)"
        lines.append(f"STORY {sid} [{status} imp={importance}] HEADLINE: {headline}\n  REASON: {reason}\n  SOURCES:\n{src_block}")
    return "\n\n".join(lines)

def _niche_context(niche: str) -> str:
    try:
        profile = load_niche(niche or "apple")
        script_cfg = profile.get("script", {}) or {}
        parts = []
        if script_cfg.get("tone"):
            parts.append(f"TONE: {script_cfg['tone']}")
        if script_cfg.get("pacing"):
            parts.append(f"PACING: {script_cfg['pacing']}")
        if script_cfg.get("word_count"):
            parts.append(f"TARGET WORDS: {script_cfg['word_count']}")
        # hooks as inspiration, not mandatory
        hooks = script_cfg.get("hooks", [])
        if hooks:
            hook_str = ", ".join(h.get("template","")[:60] for h in hooks[:3])
            parts.append(f"HOOKS: {hook_str}")
        if script_cfg.get("forbidden"):
            parts.append(f"AVOID: {', '.join(script_cfg['forbidden'])}")
        return "\n".join(parts) if parts else ""
    except Exception:
        return ""

def build_script_prompt(editorial: dict, articles: list[dict], edition: str, niche: str = "apple") -> str:
    story_ctx = _build_story_context(editorial, articles)
    niche_ctx = _niche_context(niche)
    word_target = "140 to 165 words total (intro + 3-5 stories + outro), ~60-75 seconds at 155 wpm"
    # try to get from niche
    try:
        profile = load_niche(niche)
        wc = profile.get("script", {}).get("word_count", "")
        if wc:
            word_target = f"{wc} words total (intro+stories+outro)"
    except Exception:
        pass

    return f"""You are writing a spoken Apple news briefing for YouTube Shorts (60-75 seconds, {word_target}).
Voice: spoken, natural, no clickbait caps, no "like and subscribe".

EDITION: {edition}
NICHE: {niche}
{niche_ctx}

EDITORIAL STORIES (3-5, already selected, use story_id verbatim):
{story_ctx}

TASK:
- Write intro (1 sentence, greeting + date, e.g. "Good morning, it's September 22 — three Apple headlines in 60 seconds.")
- For each story_id in order, write 1-2 sentences (12-22 words each) spoken text.
  * Use ONLY facts from SOURCES above, never invent.
  * Clearly distinguish rumor vs reported/confirmed: say "reportedly" / "according to ..." for rumor.
  * Keep citations implicit in text but preserve story_id linkage (you will return story_id).
- Write outro (1 sentence, calm close, e.g. "More tomorrow. Save this before your next update.")
- Total {word_target}. Each story segment ~12 seconds.

Output ONLY JSON:
{{"edition":"{edition}","intro":"...","stories":[{{"story_id":"s_01","text":"...","duration_target":12}}],"outro":"..."}}
- intro/outro: 1 sentence each, spoken.
- stories: in same order as editorial, story_id must match editorial exactly, text 1-2 sentences, duration_target integer seconds (estimate: words/2.6).
- No markdown, no extra fields, just JSON.
"""

def _estimate_duration(text: str) -> int:
    words = len(text.split())
    # 155 wpm = 2.58 wps
    return max(6, min(22, round(words / 2.6)))

def _validate_script(data: dict, editorial: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    if "intro" not in data or not isinstance(data["intro"], str) or len(data["intro"].split()) < 3:
        return False, "intro missing/short"
    if "outro" not in data or not isinstance(data["outro"], str) or len(data["outro"].split()) < 2:
        return False, "outro missing/short"
    stories = data.get("stories")
    if not isinstance(stories, list):
        return False, "stories not list"
    ed_stories = editorial.get("stories", [])
    if len(stories) != len(ed_stories):
        return False, f"stories count {len(stories)} != editorial {len(ed_stories)}"
    ed_ids = {s.get("story_id") for s in ed_stories}
    seen = set()
    for s in stories:
        if not isinstance(s, dict):
            return False, "story not dict"
        sid = s.get("story_id")
        if sid not in ed_ids:
            return False, f"story_id {sid} not in editorial"
        if sid in seen:
            return False, f"duplicate story_id {sid}"
        seen.add(sid)
        text = s.get("text", "")
        if not text or not isinstance(text, str) or len(text.split()) < 6:
            return False, f"story {sid} text short"
        dur = s.get("duration_target")
        if not isinstance(dur, int) or not (6 <= dur <= 30):
            return False, f"story {sid} duration_target {dur} not in 6-30"
    # total words check 80-200
    full = data["intro"] + " " + " ".join(s["text"] for s in stories) + " " + data["outro"]
    wc = len(full.split())
    if not (80 <= wc <= 220):
        return False, f"total words {wc} not in 80-220"
    return True, ""

def _fallback_script(editorial: dict, edition: str) -> dict:
    """Deterministic fallback: headlines + reason as spoken text."""
    intro = f"Good morning, it's {edition} — {len(editorial.get('stories',[]))} Apple headlines in one minute."
    stories = []
    for s in editorial.get("stories", []):
        sid = s.get("story_id")
        headline = s.get("headline", "")
        reason = s.get("reason", "")
        # simple 1-sentence from headline + reason
        text = f"{headline}. {reason}"
        # trim to ~18 words
        words = text.split()
        if len(words) > 22:
            text = " ".join(words[:22]) + "."
        stories.append({
            "story_id": sid,
            "text": text,
            "duration_target": _estimate_duration(text),
        })
    outro = "More tomorrow — save this before your next update."
    full = intro + " " + " ".join(s["text"] for s in stories) + " " + outro
    return {
        "edition": edition,
        "intro": intro,
        "stories": stories,
        "outro": outro,
        "full_script": full,
        "word_count": len(full.split()),
        "_fallback": True,
    }

def generate_script(editorial: dict, articles: list[dict], edition: str | None = None, niche: str = "apple", provider: str | None = None, max_tokens: int = 1800) -> dict:
    """Main entry: 1 LLM call, 3 attempts, validation, fallback."""
    if not editorial or not editorial.get("stories"):
        raise ValueError("No editorial stories")
    edition = edition or editorial.get("edition") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    edition = str(edition)[:10]
    niche = niche or "apple"

    base_prompt = build_script_prompt(editorial, articles, edition, niche=niche)
    prompt = base_prompt
    last_err = None
    raw_response = None
    for attempt in range(3):
        try:
            raw = call_llm(prompt, provider=provider, max_tokens=max_tokens)
            raw_response = raw
            data = _parse_script_json(raw)
            # normalize: ensure duration_target int
            for s in data.get("stories", []):
                if "duration_target" in s and not isinstance(s["duration_target"], int):
                    try:
                        s["duration_target"] = int(float(s["duration_target"]))
                    except Exception:
                        s["duration_target"] = _estimate_duration(s.get("text",""))
                # fallback if missing
                if "duration_target" not in s:
                    s["duration_target"] = _estimate_duration(s.get("text",""))
            # enrich with headline/status/sources from editorial for traceability
            ed_map = {s.get("story_id"): s for s in editorial.get("stories", [])}
            for s in data.get("stories", []):
                ed = ed_map.get(s.get("story_id"), {})
                s["headline"] = ed.get("headline", s.get("headline",""))
                s["sources"] = ed.get("sources", [])
                s["status"] = ed.get("status", "reported")
            ok, err = _validate_script(data, editorial)
            if ok:
                full = data["intro"] + " " + " ".join(x["text"] for x in data["stories"]) + " " + data["outro"]
                data["full_script"] = full
                data["word_count"] = len(full.split())
                data["_attempt"] = attempt + 1
                data["_llm_raw"] = raw[:4000]
                log(f"Script: {len(data['stories'])} stories, {data['word_count']} words (attempt {attempt+1})")
                return data
            else:
                last_err = err
                log(f"Script validation failed (attempt {attempt+1}/3): {err} — retry")
                prompt = base_prompt + f"\n\nPREVIOUS ERROR (fix it): {err}\nOutput ONLY JSON with correct story_id and duration_target."
                continue
        except json.JSONDecodeError as e:
            last_err = f"JSON parse: {e}"
            log(f"Script JSON kaputt (Versuch {attempt+1}/3): {e}")
            prompt = base_prompt + f"\n\nPREVIOUS ERROR: JSON invalid: {e}. Output ONLY JSON."
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log(f"Script LLM failed (attempt {attempt+1}/3): {e}")
            continue

    log(f"Script: alle 3 Versuche fehlgeschlagen ({last_err}) — Fallback")
    fb = _fallback_script(editorial, edition)
    fb["_llm_raw"] = (raw_response or "")[:4000]
    fb["_error"] = str(last_err)
    # enrich fallback with headline/sources/status
    ed_map = {s.get("story_id"): s for s in editorial.get("stories", [])}
    for s in fb["stories"]:
        ed = ed_map.get(s["story_id"], {})
        s["headline"] = ed.get("headline", "")
        s["sources"] = ed.get("sources", [])
        s["status"] = ed.get("status", "reported")
    return fb
