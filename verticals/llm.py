"""LLM access via LiteLLM (+ Claude CLI fallback).

Every provider goes through litellm.completion; provider-specific code is
reduced to model names, keys, and the Gemini failover chain.
Supported: claude (Anthropic), gemini (Google), openai (OpenAI), ollama
(local), minimax, litellm (explicit LITELLM_MODEL), claude_cli (Max sub).
Provider selection: --provider flag or LLM_PROVIDER env var or config.json.
"""

import json
import os
import pathlib

from .config import (
    get_anthropic_key,
    get_gemini_key,
    get_gemini_llm_model,
    get_minimax_key,
    call_claude_cli,
    has_claude_cli,
    load_config,
)
from .log import log
from .retry import with_retry


def get_provider(name: str | None = None) -> str:
    """Resolve which LLM provider to use.

    Priority: explicit name > LLM_PROVIDER env > config.json > auto-detect.
    """
    if name and name != "auto":
        return name.lower()

    from_env = os.environ.get("LLM_PROVIDER", "").lower()
    if from_env:
        return from_env

    cfg = load_config()
    from_cfg = cfg.get("LLM_PROVIDER", "").lower()
    if from_cfg:
        return from_cfg

    # Auto-detect: try providers in preference order
    if get_anthropic_key():
        return "claude"
    if get_gemini_key():
        return "gemini"
    if get_minimax_key():
        return "minimax"
    if os.environ.get("OPENAI_API_KEY") or cfg.get("OPENAI_API_KEY"):
        return "openai"
    if _ollama_available():
        return "ollama"

    # Last resort: Claude CLI
    if has_claude_cli():
        return "claude_cli"

    raise RuntimeError(
        "No LLM provider found. Set one of:\n"
        "  ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENAI_API_KEY\n"
        "  Or install Ollama with a model pulled\n"
        "  Or install Claude Code with a Max subscription"
    )


def _ollama_available() -> bool:
    """Check if Ollama is running locally."""
    try:
        import requests
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _export_config_keys():
    """Mirror config.json keys into env so litellm (and fallbacks) find them."""
    cfg = load_config()
    for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        if not os.environ.get(key) and cfg.get(key):
            os.environ[key] = cfg[key]


def _openrouter_key() -> str:
    """Resolve OPENROUTER_API_KEY (env or hermes key files)."""
    if os.environ.get("OPENROUTER_API_KEY", "").startswith("sk-or-v1-"):
        return os.environ["OPENROUTER_API_KEY"]
    # Zentraler Key (exchange/env/openrouter.env raw key, Fallback hermes/data/.env mit Prefix)
    for p in ["/srv/docker/hermes/exchange/env/openrouter.env", "/srv/docker/hermes/data/.env"]:
        try:
            txt = pathlib.Path(p).read_text().strip()
            if not txt:
                continue
            if "OPENROUTER_API_KEY=" in txt:
                cand = txt.split("OPENROUTER_API_KEY=")[1].split()[0].strip().strip('"').strip("'")
            elif txt.startswith("sk-or-v1-"):
                cand = txt.split()[0].strip()
            else:
                continue
            if cand.startswith("sk-or-v1-"):
                os.environ["OPENROUTER_API_KEY"] = cand
                return cand
        except Exception:
            pass
    return os.environ.get("OPENROUTER_API_KEY", "")


def _openrouter_fallbacks() -> list:
    """Smart routing: dynamisch beste Modelle (kein Hardcode).

    - PAID_FALLBACK env überschreibt (manuell pin wenn nötig)
    - sonst model_routing.get_paid_fallbacks() live via OpenRouter (Cache 1h, Score: Preis+JSON+Kontext)
    - FALLBACK_MODELS env überschreibt freie Modelle, sonst keine Free per Default (instabil 22.09.)
    """
    _openrouter_key()
    # 1. Manueller Pin hat Vorrang (für Notfälle)
    paid_env = os.environ.get("PAID_FALLBACK", "").strip()
    if paid_env:
        paid_list = [p.strip() for p in paid_env.split(",") if p.strip()]
        # wenn PAID_FALLBACK mehrere enthält, nutze die Liste
        return paid_list
    # 2. Smart routing: dynamisch beste 3 (kein Hardcode)
    try:
        from .model_routing import get_paid_fallbacks
        smart = get_paid_fallbacks()
        if smart:
            return smart
    except Exception as e:
        log(f"Smart routing fehlgeschlagen {e} — Fallback auf minimal")
    # 3. Minimal fallback (wenn API offline)
    return ["openrouter/openai/gpt-4o-mini", "openrouter/qwen/qwen-2.5-7b-instruct"]


def _ollama_model() -> str:
    """Pick best local Ollama model — dynamisch, kein Hardcode-Pin (ponytail: erstes verfügbares)."""
    import requests
    try:
        tags = requests.get("http://localhost:11434/api/tags", timeout=5).json()
        available = [m["name"] for m in tags.get("models", [])]
    except Exception:
        raise RuntimeError("Ollama not running. Start with: ollama serve")
    if not available:
        raise RuntimeError("No Ollama models found. Pull one: ollama pull llama3.1:8b")
    # ponytail: nimm erstes verfügbares, kein Hardcode-Ranking — lokales Modell ist eh User-Choice
    log(f"Using Ollama model: {available[0]} (aus {len(available)} verfügbaren)")
    return f"ollama/{available[0]}"


@with_retry(max_retries=2, base_delay=3.0)
def call_llm(prompt: str, provider: str | None = None, max_tokens: int = 1500) -> str:
    """Call any supported LLM provider with the given prompt.

    Args:
        prompt: The full prompt text.
        provider: Provider name (claude, gemini, openai, ollama, minimax, litellm, claude_cli).
        max_tokens: Maximum response tokens.

    Returns:
        The LLM response text.
    """
    provider = get_provider(provider)
    log(f"Calling LLM via {provider}...")
    _export_config_keys()

    if provider == "claude_cli":
        return call_claude_cli(prompt, max_tokens=max_tokens)
    if provider == "claude":
        if get_anthropic_key():
            # smart: best anthropic via routing, kein Hardcode claude-sonnet-4-6
            try:
                from .model_routing import get_best_for_provider
                best = get_best_for_provider("anthropic", limit=1)
                # best is openrouter/anthropic/... -> strip to anthropic/...
                model = best[0].replace("openrouter/", "") if best else "anthropic/claude-sonnet-4-6"
                if not model.startswith("anthropic/"):
                    model = "anthropic/" + model.split("/")[-1]
            except Exception:
                model = "anthropic/claude-sonnet-4-6"
            return _call_litellm(prompt, max_tokens, model=model, api_key=get_anthropic_key())
        if has_claude_cli():
            return call_claude_cli(prompt, max_tokens=max_tokens)
        raise RuntimeError("No Claude access found. Set ANTHROPIC_API_KEY or install Claude Code.")
    if provider == "gemini":
        api_key = get_gemini_key()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        max_tokens = int(os.environ.get("GEMINI_MAX_TOKENS", max_tokens))
        # smart: best google model via routing, fallback auf env oder Auto
        try:
            from .model_routing import get_best_for_provider
            best_g = get_best_for_provider("google", limit=1)
            if best_g:
                # openrouter/google/gemini-... -> gemini/...
                gemini_model = best_g[0].replace("openrouter/", "")
                # ensure gemini/ prefix for litellm
                if not gemini_model.startswith("gemini/"):
                    gemini_model = f"gemini/{gemini_model.split('/')[-1]}"
            else:
                gemini_model = f"gemini/{get_gemini_llm_model()}"
        except Exception:
            gemini_model = f"gemini/{get_gemini_llm_model()}"
        try:
            return _call_litellm(prompt, max_tokens, model=gemini_model, api_key=api_key)
        except Exception as e:
            log(f"Gemini {gemini_model} failed: {e} -> trying OpenRouter smart fallbacks")
            o_key = _openrouter_key()
            if not o_key:
                raise
            for fb in _openrouter_fallbacks():
                try:
                    log(f"Trying fallback {fb}...")
                    return _call_litellm(prompt, max_tokens, model=fb, api_key=o_key)
                except Exception as fe:
                    log(f"Fallback {fb} failed: {fe}")
                    continue
            raise
    if provider == "minimax":
        api_key = get_minimax_key()
        if not api_key:
            raise RuntimeError("MINIMAX_API_KEY not set")
        # smart: best minimax via routing, fallback auf bekanntes
        try:
            from .model_routing import get_best_for_provider
            best_m = get_best_for_provider("minimax", limit=1)
            model_m = best_m[0].replace("openrouter/", "") if best_m else "MiniMax-M2.7"
            if "minimax" not in model_m.lower():
                model_m = "MiniMax-M2.7"
        except Exception:
            model_m = "MiniMax-M2.7"
        return _call_litellm(prompt, max_tokens, model=f"openai/{model_m}",
                             api_key=api_key,
                             api_base=os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1"),
                             temperature=1.0)
    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY") or load_config().get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        try:
            from .model_routing import get_best_for_provider
            best_o = get_best_for_provider("openai", limit=1)
            model_o = best_o[0].replace("openrouter/", "") if best_o else "openai/gpt-4o-mini"
            if not model_o.startswith("openai/"):
                model_o = f"openai/{model_o.split('/')[-1]}"
        except Exception:
            model_o = "openai/gpt-4o-mini"
        return _call_litellm(prompt, max_tokens, model=model_o, api_key=api_key)
    if provider == "ollama":
        return _call_litellm(prompt, max_tokens, model=_ollama_model(), timeout=120)
    if provider == "litellm":
        return _call_litellm(prompt, max_tokens)
    raise ValueError(f"Unknown LLM provider: {provider}")


def _call_litellm(prompt: str, max_tokens: int, model: str | None = None,
                  fallbacks: list | None = None, api_key: str | None = None,
                  api_base: str | None = None, temperature: float = 0.7,
                  timeout: int | None = None) -> str:
    """Call any LLM provider via the litellm SDK — smart routing, kein Hardcode-Pin."""
    import litellm

    if not model:
        # smart: best general model dynamisch (kein Hardcode gpt-4o)
        try:
            from .model_routing import get_best_models
            best = get_best_models(task="general", limit=1)
            model = best[0] if best else os.environ.get("LITELLM_MODEL", "openrouter/openai/gpt-4o-mini")
        except Exception:
            model = os.environ.get("LITELLM_MODEL", "openrouter/openai/gpt-4o-mini")
    log(f"Using LiteLLM model: {model}")
    max_tokens = int(os.environ.get("LITELLM_MAX_TOKENS", max_tokens))

    kwargs: dict = dict(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
        drop_params=True,
    )
    if fallbacks:
        kwargs["fallbacks"] = fallbacks
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    if timeout:
        kwargs["timeout"] = timeout

    response = litellm.completion(**kwargs)

    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("Empty response from LiteLLM")
    return content.strip()
