"""Visual Plan — LLM beschreibt welche Visuals gebraucht werden (keine URLs).

Input:  script.json + editorial.json + articles.json (+ niche)
Output: {edition, shots:[{story_id, idx, duration, subject, asset_type, preferred_source, description}]}
Deterministisch validiert, halluzinierte URLs werden nicht erzeugt.
"""

import json
import re
from datetime import datetime, timezone

from .log import log
from .llm import call_llm
from .niche import load_niche

ALLOWED_ASSET_TYPES = {
    "official_product_image",
    "official_screenshot",
    "press_image",
    "title_card",
}

ALLOWED_SOURCES = {
    "apple.com",
    "macrumors.com",
    "9to5mac.com",
    "appleinsider.com",
    "cultofmac.com",
}

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
    depth_brace = depth_bracket = 0
    in_str = esc = False
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

def _parse_visual_json(raw: str) -> dict:
    raw = _strip_fences(raw)
    start = raw.find("{")
    end = raw.rfind("}") + 1
    candidate = raw[start:end] if start >= 0 and end > start else raw
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        repaired = _repair_json(candidate)
        return json.loads(repaired)

def _niche_visual_context(niche: str) -> str:
    try:
        profile = load_niche(niche or "apple")
        vis = profile.get("visuals", {}) or {}
        parts = []
        if vis.get("style"):
            parts.append(f"STYLE: {vis['style']}")
        if vis.get("mood"):
            parts.append(f"MOOD: {vis['mood']}")
        # prompt_suffix not needed for plan, but style helps
        return "\n".join(parts)
    except Exception:
        return ""

def build_visual_prompt(script: dict, editorial: dict, edition: str, niche: str = "apple") -> str:
    # story context
    ed_map = {s.get("story_id"): s for s in editorial.get("stories", [])}
    lines = []
    for s in script.get("stories", []):
        sid = s.get("story_id")
        ed = ed_map.get(sid, {})
        headline = ed.get("headline", s.get("headline", ""))
        status = ed.get("status", s.get("status", ""))
        text = s.get("text", "")[:200]
        dur = s.get("duration_target", 12)
        lines.append(f"STORY {sid} [{status}] dur={dur}s HEADLINE: {headline}\n  TEXT: {text}")
    story_block = "\n\n".join(lines)
    niche_ctx = _niche_visual_context(niche)
    intro = script.get("intro", "")[:150]
    outro = script.get("outro", "")[:100]
    return f"""You are a visual director for an Apple news Short (9:16, 1080x1920).
TASK: For each story_id, describe ONE visual (no URLs, no invented image links).

EDITION: {edition}
NICHE: {niche}
{niche_ctx}

INTRO: {intro}
OUTRO: {outro}

STORIES (in order, with duration):
{story_block}

Rules:
- One shot per story_id, plus optional intro/outro title_card if useful.
- For each shot, specify:
  * subject: 2-5 words, what is shown (e.g. "iPhone 18 Pro camera module")
  * asset_type: one of {', '.join(sorted(ALLOWED_ASSET_TYPES))} — prefer official_product_image or official_screenshot for Apple, press_image for news sites, title_card for intro/outro.
  * preferred_source: one of {', '.join(sorted(ALLOWED_SOURCES))} — apple.com first for Apple products, then macrumors/9to5 for news.
  * description: 1 sentence, photorealistic detail for resolver (e.g. "close-up of iPhone 18 Pro titanium frame, studio lighting, 4K").
  * duration: integer seconds, should match story duration_target (6-22) or 3 for title_card.

Output ONLY JSON:
{{"edition":"{edition}","shots":[{{"story_id":"s_01","idx":0,"duration":12,"subject":"iPhone 18 Pro","asset_type":"official_product_image","preferred_source":"apple.com","description":"..."}}]}}
- idx: 0-based order, sequential.
- story_id must be from STORIES above (or "intro"/"outro" for title cards).
- duration: int 3-22.
- Do NOT include url, image link, or html.
"""

def _validate_visual(data: dict, editorial: dict, script: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "not a dict"
    edition = data.get("edition", "")
    if not edition or not isinstance(edition, str):
        return False, "edition missing"
    shots = data.get("shots")
    if not isinstance(shots, list) or not shots:
        return False, "shots missing/empty"
    # allow 3-7 shots (3-5 stories + intro/outro)
    if not (3 <= len(shots) <= 7):
        return False, f"shots count {len(shots)} not in 3-7"
    ed_ids = {s.get("story_id") for s in editorial.get("stories", [])}
    ed_ids.add("intro")
    ed_ids.add("outro")
    seen_idx = set()
    for i, sh in enumerate(shots):
        if not isinstance(sh, dict):
            return False, f"shot {i} not dict"
        sid = sh.get("story_id")
        if sid not in ed_ids:
            return False, f"shot {i} story_id {sid} not in editorial/intro/outro"
        idx = sh.get("idx")
        if not isinstance(idx, int) or idx != i:
            return False, f"shot {i} idx {idx} != {i}"
        if idx in seen_idx:
            return False, f"duplicate idx {idx}"
        seen_idx.add(idx)
        dur = sh.get("duration")
        if not isinstance(dur, int) or not (3 <= dur <= 22):
            return False, f"shot {i} duration {dur} not in 3-22"
        subj = sh.get("subject", "")
        if not subj or not isinstance(subj, str) or len(subj.split()) < 1:
            return False, f"shot {i} subject missing"
        atype = sh.get("asset_type")
        if atype not in ALLOWED_ASSET_TYPES:
            return False, f"shot {i} asset_type {atype} not in {ALLOWED_ASSET_TYPES}"
        src = sh.get("preferred_source")
        if src not in ALLOWED_SOURCES:
            return False, f"shot {i} preferred_source {src} not in {ALLOWED_SOURCES}"
        desc = sh.get("description", "")
        if not desc or not isinstance(desc, str) or len(desc.split()) < 4:
            return False, f"shot {i} description short"
        # forbid URLs in description
        if "http" in desc.lower():
            return False, f"shot {i} description contains URL"
    return True, ""

def _fallback_visual(editorial: dict, script: dict, edition: str, niche: str = "apple") -> dict:
    """Deterministic: 1 shot per story, official_product_image apple.com, title_card for intro/outro if needed."""
    shots = []
    # intro title_card
    shots.append({
        "story_id": "intro",
        "idx": 0,
        "duration": 3,
        "subject": "Apple news intro",
        "asset_type": "title_card",
        "preferred_source": "apple.com",
        "description": "Minimal title card with Apple logo, clean white studio lighting, 4K",
    })
    idx = 1
    # use script duration or editorial importance to estimate
    for s in script.get("stories", []):
        sid = s.get("story_id")
        # try to infer subject from headline
        headline = next((e.get("headline","") for e in editorial.get("stories", []) if e.get("story_id")==sid), s.get("headline",""))
        # simple subject: first 3 words of headline
        subj = " ".join(headline.split()[:4])[:40] or "Apple product"
        dur = s.get("duration_target", 12)
        # clamp 6-22
        dur = max(6, min(22, dur))
        shots.append({
            "story_id": sid,
            "idx": idx,
            "duration": dur,
            "subject": subj,
            "asset_type": "official_product_image",
            "preferred_source": "apple.com",
            "description": f"Official Apple product image for {subj}, studio lighting, 4K, clean background",
        })
        idx += 1
    shots.append({
        "story_id": "outro",
        "idx": idx,
        "duration": 3,
        "subject": "Apple news outro",
        "asset_type": "title_card",
        "preferred_source": "apple.com",
        "description": "Minimal outro title card with Apple logo and 'More tomorrow', clean, 4K",
    })
    return {"edition": edition, "shots": shots, "_fallback": True}

def generate_visual_plan(script: dict, editorial: dict, edition: str | None = None, niche: str = "apple", provider: str | None = None, max_tokens: int = 1500) -> dict:
    """Main entry: 1 LLM, 3 attempts, validation, fallback."""
    if not script or not script.get("stories"):
        raise ValueError("No script stories")
    if not editorial or not editorial.get("stories"):
        raise ValueError("No editorial")
    edition = edition or script.get("edition") or editorial.get("edition") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    edition = str(edition)[:10]
    niche = niche or "apple"

    base_prompt = build_visual_prompt(script, editorial, edition, niche=niche)
    prompt = base_prompt
    last_err = None
    raw_response = None
    for attempt in range(3):
        try:
            raw = call_llm(prompt, provider=provider, max_tokens=max_tokens)
            raw_response = raw
            data = _parse_visual_json(raw)
            # normalize: ensure idx sequential
            for i, sh in enumerate(data.get("shots", [])):
                sh["idx"] = i
            ok, err = _validate_visual(data, editorial, script)
            if ok:
                data["_attempt"] = attempt + 1
                data["_llm_raw"] = raw[:4000]
                log(f"Visual: {len(data['shots'])} shots (attempt {attempt+1})")
                return data
            else:
                last_err = err
                log(f"Visual validation failed (attempt {attempt+1}/3): {err} — retry")
                prompt = base_prompt + f"\n\nPREVIOUS ERROR (fix it): {err}\nRemember: asset_type in {ALLOWED_ASSET_TYPES}, preferred_source in {ALLOWED_SOURCES}, no URLs."
                continue
        except json.JSONDecodeError as e:
            last_err = f"JSON parse: {e}"
            log(f"Visual JSON kaputt (Versuch {attempt+1}/3): {e}")
            prompt = base_prompt + f"\n\nPREVIOUS ERROR: JSON invalid: {e}. Output ONLY JSON."
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log(f"Visual LLM failed (attempt {attempt+1}/3): {e}")
            continue

    log(f"Visual: alle 3 Versuche fehlgeschlagen ({last_err}) — Fallback")
    fb = _fallback_visual(editorial, script, edition, niche=niche)
    fb["_llm_raw"] = (raw_response or "")[:4000]
    fb["_error"] = str(last_err)
    return fb
