"""Editorial Selection — LLM wählt 3-5 Stories aus normalisiertem Pool.

LLM entscheidet, Code validiert/speichert. Kein Web-Zugriff, kein Erfinden.

Input:  articles (aus runs/<ts>/articles.json), optional community signals
Output: {edition, stories:[{story_id, headline, importance, status, sources, reason, reddit_signal?}]}
"""

import json
import re
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .log import log
from .llm import call_llm


ALLOWED_STATUSES = {"confirmed", "reported", "rumor", "community"}


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
    """Minimal repair: close open string/brackets (ponytail: from draft.py)."""
    s = raw.strip()
    start = s.find("{")
    if start > 0:
        s = s[start:]
    # close open string
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


def _parse_editorial_json(raw: str) -> dict:
    raw = _strip_fences(raw)
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start >= 0 and end > start:
        candidate = raw[start:end]
    else:
        candidate = raw
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        repaired = _repair_json(candidate)
        return json.loads(repaired)


def _build_pool_text(articles: list[dict], max_len: int = 220) -> str:
    lines = []
    for i, a in enumerate(articles, 1):
        title = a.get("title", "")[:140]
        summary = a.get("summary", "")[:max_len]
        pub = a.get("published_at", "")[:10]
        src = a.get("source", "")
        aid = a.get("article_id", "")
        # ponytail: short summary to keep prompt <3000 tokens
        lines.append(f"{i}. ID={aid} SRC={src} DATE={pub} TITLE={title} SUMMARY={summary}")
    return "\n".join(lines)


def _build_community_text(signals: list[dict], max_signals: int = 10) -> str:
    if not signals:
        return "(keine Reddit-Signale)"
    lines = []
    for s in signals[:max_signals]:
        title = s.get("title", "")[:200]
        sub = s.get("subreddit", "")
        url = s.get("url", "")
        lines.append(f"- [r/{sub}] {title} | {url}")
    return "\n".join(lines)


def build_prompt(articles: list[dict], community: list[dict], edition: str, niche: str = "apple") -> str:
    pool = _build_pool_text(articles)
    comm = _build_community_text(community)
    allowed = ", ".join(a.get("article_id","") for a in articles)
    if niche in ("musik", "music", "education"):
        n = len(articles)
        want = "genau 1 Story" if n <= 2 else "1-3 Stories"
        return f"""Du bist Musiklehrer-Redakteur für 60-Sekunden Erklärvideos (deutsch, kein Denglisch).
AUFGABE: Wähle {want} aus POOL. IDs exakt kopieren. Nichts erfinden. Alles auf Deutsch.

POOL ({len(articles)} Themen):
{pool}

ERLAUBTE IDS (exakt kopieren):
{allowed}

EDITION: {edition}

REGELN:
- HEADLINE 5-10 Wörter, deutsch, sachlich, kein Clickbait.
- SOURCES: 1 article_id aus ALLOWED IDS, exakt kopieren.
- STATUS: immer "reported" (eigene Themen, keine Gerüchte).
- IMPORTANCE 0.5-0.9.
- REASON 1 Satz deutsch.
- KEIN Apple, KEIN iPhone erfinden — nur das Thema aus POOL.

NUR JSON:
{{"edition":"{edition}","stories":[{{"story_id":"s_01","headline":"...","importance":0.7,"status":"reported","sources":["ID1"],"reason":"..."}}]}}
"""
    return f"""You are an Apple news editor for a 60-second briefing (Frontrunners style).
TASK: Select exactly 3-5 stories from POOL. Copy IDs verbatim. Never invent.

POOL ({len(articles)} articles, IDs must be copied exactly):
{pool}

ALLOWED IDS (copy verbatim, exactly as shown, no other IDs allowed):
{allowed}

COMMUNITY SIGNALS (Reddit, boost only, never confirmed alone):
{comm}

EDITION: {edition}

RULES (strict):
- HEADLINE 8-14 words, factual, no caps clickbait.
- SOURCES: 1-3 article_ids, MUST be from ALLOWED IDS list above, copy character-for-character. If you invent an ID, you FAIL.
- STATUS: confirmed (≥2 primary sources or official Apple), reported (1 primary), rumor (leak), community (only Reddit).
- IMPORTANCE 0.0-1.0.
- REASON 1 sentence with recency/source/community.
- DEDUP: same event → 1 story, merge sources.
- RECENCY: prefer last 48h. RELEVANCE: Apple product/iOS/macOS/watchOS.

Output ONLY JSON:
{{"edition":"{edition}","stories":[{{"story_id":"s_01","headline":"...","importance":0.87,"status":"reported","sources":["ID1"],"reason":"..."}}]}}
No markdown, no explanation, just JSON.
"""


def _try_fix_hallucinated(data: dict, articles: list[dict]) -> dict:
    """If LLM hallucinated IDs, try to map by headline word overlap (ponytail: lenient repair)."""
    article_ids = {a.get("article_id"): a for a in articles}
    # build title index by words
    for s in data.get("stories", []):
        fixed = []
        for src in s.get("sources", []):
            if src in article_ids:
                fixed.append(src)
            else:
                # try to find article whose title words overlap headline
                headline = s.get("headline", "").lower()
                h_words = set(re.findall(r"[a-z0-9]{4,}", headline))
                best = None
                best_overlap = 0
                for aid, art in article_ids.items():
                    t_words = set(re.findall(r"[a-z0-9]{4,}", art.get("title","").lower()))
                    inter = len(h_words & t_words)
                    if inter > best_overlap:
                        best_overlap = inter
                        best = aid
                if best and best_overlap >= 2:
                    fixed.append(best)
                    log(f"Fix hallucinated {src} -> {best} via headline '{headline[:40]}'")
                else:
                    # fallback to first article (will still validate? but we keep)
                    # use newest article
                    newest = sorted(articles, key=lambda a: a.get("published_at") or "", reverse=True)[0].get("article_id")
                    fixed.append(newest)
                    log(f"Fix hallucinated {src} -> fallback {newest}")
        s["sources"] = fixed[:3]
    return data


def _validate(edition_data: dict, articles: list[dict], edition_expected: str) -> tuple[bool, str]:
    """Validate editorial JSON, returns (ok, error)."""
    if not isinstance(edition_data, dict):
        return False, "not a dict"
    edition = edition_data.get("edition", "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(edition)):
        return False, f"edition format invalid: {edition} (expected YYYY-MM-DD)"
    stories = edition_data.get("stories")
    if not isinstance(stories, list):
        return False, "stories not a list"
    # musik/manual: 1 Artikel -> 1 Story ok, sonst 3-5
    if len(articles) <= 2:
        if not (1 <= len(stories) <= 3):
            return False, f"stories count {len(stories)} not in 1-3 (small pool)"
    elif not (3 <= len(stories) <= 5):
        return False, f"stories count {len(stories)} not in 3-5"
    article_ids = {a.get("article_id") for a in articles}
    seen_ids = set()
    for idx, s in enumerate(stories):
        if not isinstance(s, dict):
            return False, f"story {idx} not a dict"
        sid = s.get("story_id", "")
        if not sid or not isinstance(sid, str):
            return False, f"story {idx} missing story_id"
        if sid in seen_ids:
            return False, f"duplicate story_id {sid}"
        seen_ids.add(sid)
        headline = s.get("headline", "")
        if not headline or not isinstance(headline, str) or len(headline.split()) < 3:
            return False, f"story {sid} headline invalid"
        imp = s.get("importance")
        if not isinstance(imp, (int, float)) or not (0.0 <= imp <= 1.0):
            return False, f"story {sid} importance {imp} not in 0-1"
        status = s.get("status", "")
        if status not in ALLOWED_STATUSES:
            return False, f"story {sid} status {status} not in {ALLOWED_STATUSES}"
        sources = s.get("sources")
        if not isinstance(sources, list) or not sources:
            return False, f"story {sid} sources empty"
        for src in sources:
            if src not in article_ids:
                return False, f"story {sid} source {src} not in pool (hallucinated)"
        reason = s.get("reason", "")
        if not reason or not isinstance(reason, str):
            return False, f"story {sid} reason missing"
    return True, ""


def _fallback_selection(articles: list[dict], edition: str) -> dict:
    """Deterministic fallback: newest 3 by published_at, each single source."""
    sorted_arts = sorted(articles, key=lambda a: a.get("published_at") or "", reverse=True)
    stories = []
    for i, a in enumerate(sorted_arts[:3], 1):
        stories.append({
            "story_id": f"s_0{i}",
            "headline": a.get("title", "")[:100],
            "importance": round(0.6 - i*0.05, 2),
            "status": "reported",
            "sources": [a.get("article_id")],
            "reason": f"Fallback: newest article by published_at ({a.get('published_at','')[:10]})",
        })
    return {"edition": edition, "stories": stories, "_fallback": True}


def select_editorial(articles: list[dict], community: list[dict] | None = None, edition: str | None = None, provider: str | None = None, max_tokens: int = 2200, niche: str = "apple") -> dict:
    """Main entry: 1 LLM call, 3 attempts, validation+repair, fallback."""
    community = community or []
    if not articles:
        raise ValueError("No articles in pool")
    if not edition:
        edition = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        edition = str(edition)[:10]

    base_prompt = build_prompt(articles, community, edition, niche=niche)
    prompt = base_prompt
    last_err = None
    raw_response = None
    for attempt in range(3):
        try:
            raw = call_llm(prompt, provider=provider, max_tokens=max_tokens)
            raw_response = raw
            data = _parse_editorial_json(raw)
            # try repair hallucinated before validation
            data = _try_fix_hallucinated(data, articles)
            ok, err = _validate(data, articles, edition)
            if ok:
                data["_llm_raw"] = raw[:4000]
                data["_attempt"] = attempt + 1
                log(f"Editorial: {len(data['stories'])} stories (attempt {attempt+1}), provider {provider or 'auto'}")
                return data
            else:
                last_err = err
                log(f"Editorial validation failed (attempt {attempt+1}/3): {err} — retry")
                # feed error back into prompt for next attempt (ponytail: lenient)
                prompt = base_prompt + f"\n\nPREVIOUS ERROR (fix it): {err}\nRemember: copy IDs exactly from ALLOWED IDS."
                continue
        except json.JSONDecodeError as e:
            last_err = f"JSON parse: {e}"
            log(f"Editorial JSON kaputt (Versuch {attempt+1}/3): {e}")
            prompt = base_prompt + f"\n\nPREVIOUS ERROR: JSON invalid: {e}. Output ONLY JSON."
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log(f"Editorial LLM failed (attempt {attempt+1}/3): {e}")
            continue

    log(f"Editorial: alle 3 Versuche fehlgeschlagen ({last_err}) — Fallback deterministisch")
    fb = _fallback_selection(articles, edition)
    fb["_llm_raw"] = (raw_response or "")[:4000]
    fb["_error"] = str(last_err)
    return fb
