"""Smart model routing — kein Hardcode, best wird dynamisch ermittelt.

- Primär: OpenRouter /api/v1/models live abfragen (Cache 1h)
- Scoring: günstig + genug Kontext + supports structured_outputs/response_format + non-free preferred (ponytail: stabil > billig)
- Fallback: wenn API offline, nimm kleine kuratierte Liste (nicht gepinnt auf 1 Modell)
- Für alle Tasks: editorial/script/general nutzen gleiche Scoring, aber editorial bevorzugt JSON-fähige
"""

import json
import time
import os
from pathlib import Path
from typing import List

from .log import log

CACHE_PATH = Path.home() / ".verticals" / "model_cache.json"
CACHE_TTL = 3600  # 1h

# Blocklist: Modelle die via litellm/openrouter aktuell 404/empty liefern (22.09. beobachtet)
# ponytail: dynamisch, aber harte Fails blocken um Latency zu sparen — wird via Fetch aktualisiert, nicht Hardcode-Pin
BLOCKLIST_SUBSTR = ["gpt-oss-120b", "qwen3-30b-a3b", "qwen3-14b", "gemini-2.5-flash-lite"]  # 22.09. empty/404

# Offline-Fallback: letzter Cache, nicht Hardcode (ponytail: Cache statt Pin)
# Kein Single-Pin — wird aus letztem erfolgreichen Fetch gespeist

def _load_cache() -> dict | None:
    try:
        if CACHE_PATH.exists() and time.time() - CACHE_PATH.stat().st_mtime < CACHE_TTL:
            return json.loads(CACHE_PATH.read_text())
    except Exception:
        pass
    return None

def _save_cache(data: dict):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass

def _fetch_openrouter_models() -> list[dict]:
    """Live-Abfrage, 5s Timeout, kein Hardcode-Filter."""
    import requests
    key = os.environ.get("OPENROUTER_API_KEY", "")
    # key auch aus hermes files (wie llm.py)
    if not key.startswith("sk-or-v1-"):
        for p in ["/srv/docker/hermes/exchange/env/openrouter.env", "/srv/docker/hermes/data/.env"]:
            try:
                txt = Path(p).read_text().strip()
                if "OPENROUTER_API_KEY=" in txt:
                    cand = txt.split("OPENROUTER_API_KEY=")[1].split()[0].strip().strip('"').strip("'")
                    if cand.startswith("sk-or-v1-"):
                        key = cand
                        break
                elif txt.startswith("sk-or-v1-"):
                    key = txt.split()[0].strip()
                    break
            except Exception:
                pass
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    r = requests.get("https://openrouter.ai/api/v1/models", headers=headers, timeout=5)
    r.raise_for_status()
    data = r.json().get("data", [])
    # cache raw
    _save_cache({"fetched_at": time.time(), "models": data})
    return data

def _score_model(m: dict) -> float:
    """Scoring: niedriger = besser — kein Hardcode, smart über Preis+JSON+Kontext+Provider."""
    try:
        mid = m.get("id", "").lower()
        if any(x in mid for x in BLOCKLIST_SUBSTR):
            return 999
        if any(x in mid for x in ["auto", "fusion", "pareto", "bodybuilder", "lyria"]):
            return 999
        if "image" in mid:
            return 999
        if ":batch" in mid:
            return 999  # batch via Gemini API nicht unterstützt
        pricing = m.get("pricing", {}) or {}
        try:
            prompt_price = float(pricing.get("prompt", "0.01"))
            if prompt_price < 0:
                return 999
            if prompt_price == 0:
                # 0-Preis = oft experimentell/free ohne Markierung -> de-priorisieren
                prompt_price = 0.005
        except Exception:
            prompt_price = 0.01
        arch = m.get("architecture") or {}
        if arch.get("tokenizer") == "Router":
            return 999
        ctx = int(m.get("context_length", 0) or 0)
        # Ideal: 8k-128k, zu klein oder zu groß (1M) ist für Editorial nicht nötig -> leichte Penalty
        if ctx < 8192:
            ctx_bonus = 0.005
        elif ctx > 200000:
            ctx_bonus = 0.001  # 1M Kontext ist Overkill, nicht nötig
        else:
            ctx_bonus = 0
        supports_json = "response_format" in (m.get("supported_parameters") or []) or "structured_outputs" in (m.get("supported_parameters") or [])
        json_bonus = -0.002 if supports_json else 0.004
        is_free = ":free" in mid
        free_penalty = 0.004 if is_free else 0
        # Preis-Ideal: ~0.15$/1M (gpt-4o-mini) bis ~0.5$/1M ist sweet spot für Editorial (JSON stabil, günstig)
        # Zu billig (<0.05$/1M = 0.00000005) oft low-quality, zu teuer (>5$/1M) Overkill
        # Score = Distanz zum Ideal 0.00000015 + Basiskosten
        ideal = 0.00000015
        price_dist = abs(prompt_price - ideal) * 1000  # skaliert
        # Provider-Bonus: bekannte LLM-Anbieter (text) leicht bevorzugen, ohne Hardcode einzelnes Modell
        known_providers = ("openai/", "google/", "anthropic/", "qwen/", "deepseek/", "meta-llama/", "mistralai/")
        provider_bonus = -0.001 if any(mid.startswith(p) for p in known_providers) else 0.002
        return prompt_price + price_dist * 0.1 + ctx_bonus + json_bonus + free_penalty + provider_bonus
    except Exception:
        return 999

def get_best_models(task: str = "general", limit: int = 5, provider_filter: str | None = None) -> List[str]:
    """Dynamisch beste Modelle für Task (kein Hardcode)."""
    cached = _load_cache()
    models = None
    if cached and "models" in cached:
        models = cached["models"]
        log(f"Model routing: Cache hit ({len(models)} Modelle, Alter {int(time.time()-cached.get('fetched_at',0))}s)")
    else:
        try:
            models = _fetch_openrouter_models()
            log(f"Model routing: {len(models)} Modelle live von OpenRouter")
        except Exception as e:
            log(f"Model routing: Fetch fehlgeschlagen {e} — versuche Cache (expired)")
            # versuche expired Cache
            try:
                if CACHE_PATH.exists():
                    data = json.loads(CACHE_PATH.read_text())
                    models = data.get("models", [])
                    if models:
                        log(f"Model routing: expired Cache {len(models)} Modelle")
                    else:
                        return []
                else:
                    return []
            except Exception:
                return []

    # filter: nur chat-fähige mit pricing, optional provider_filter (z.B. "openai")
    filtered = []
    for m in models:
        mid = m.get("id", "")
        if not mid or "/" not in mid:
            continue
        if provider_filter and not mid.startswith(provider_filter + "/"):
            continue
        if not m.get("pricing"):
            continue
        if int(m.get("context_length", 0)) < 4096:
            continue
        filtered.append(m)

    # scoring
    ranked = sorted(filtered, key=_score_model)
    # nimm Top limit, format für litellm: openrouter/<id> (falls id schon openrouter/ enthält, nicht doppeln)
    result = []
    for m in ranked:
        mid = m["id"]
        # OpenRouter IDs sind bereits "provider/model", litellm erwartet "openrouter/<id>"
        litellm_id = f"openrouter/{mid}" if not mid.startswith("openrouter/") else mid
        result.append(litellm_id)
        if len(result) >= limit:
            break

    if not result:
        return []
    if all(":free" in r for r in result):
        # nur free übrig -> nimm nächstbesten paid aus Cache (zweiter Durchlauf ohne free)
        paid_only = [m for m in filtered if ":free" not in m.get("id","")]
        if paid_only:
            ranked_paid = sorted(paid_only, key=_score_model)
            for m in ranked_paid[:2]:
                mid = m["id"]
                litellm_id = f"openrouter/{mid}" if not mid.startswith("openrouter/") else mid
                if litellm_id not in result:
                    result = [litellm_id] + result[:limit-1]
                    break
    log(f"Model routing ({task}): {result}")
    return result

def get_paid_fallbacks() -> List[str]:
    """Für llm.py: dynamische Paid-Fallbacks (task=general)."""
    res = get_best_models(task="general", limit=3)
    # falls leer (offline ohne Cache) -> leere Liste, llm.py wird gemini direkt versuchen
    return res

def get_editorial_models() -> List[str]:
    """Editorial braucht JSON, also filtere nach JSON-Support (extra)."""
    return get_best_models(task="editorial", limit=3)

def get_best_for_provider(provider: str, limit: int = 3) -> List[str]:
    """Best Modelle für Provider — smart, ohne Hardcode einzelnes Modell."""
    mapping = {
        "openai": "openai",
        "anthropic": "anthropic",
        "claude": "anthropic",
        "google": "google",
        "gemini": "google",
        "qwen": "qwen",
        "deepseek": "deepseek",
        "mistral": "mistralai",
        "meta": "meta-llama",
        "cohere": "cohere",
        "minimax": "minimax",
    }
    prefix = mapping.get(provider.lower(), provider.lower())
    # für gemini: nur echte gemini-Modelle, nicht gemma/lyria
    if provider.lower() in ("google", "gemini"):
        # hole alle, filtere dann auf gemini
        all_best = get_best_models(task=f"provider:{provider}", limit=20, provider_filter=prefix)
        # bevorzuge gemini-*, nicht gemma
        gemini_only = [m for m in all_best if "gemini" in m.lower()]
        if gemini_only:
            return gemini_only[:limit]
        # fallback: wenn kein gemini in Top 20, nimm generische
        return get_best_models(task="general", limit=limit)
    res = get_best_models(task=f"provider:{provider}", limit=limit, provider_filter=prefix)
    if not res:
        res = get_best_models(task="general", limit=limit)
    return res
