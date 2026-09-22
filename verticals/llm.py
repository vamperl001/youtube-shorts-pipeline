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
    """Failover chain: BEZAHLT zuerst, Free-Modelle nur als letzte Reserve.
    (User-Entscheidung 15.09.: lieber ~2ct/Video als trunkierte Drafts, 22.09. paid auf gpt-4o-mini gepinnt weil qwen 3.5 empty)."""
    _openrouter_key()
    paid = os.environ.get("PAID_FALLBACK", "openrouter/openai/gpt-4o-mini")
    fb = os.environ.get("FALLBACK_MODELS", "")
    if fb:
        try:
            free_models = json.loads(fb) if fb.strip().startswith("[") else [x.strip() for x in fb.split(",") if x.strip()]
        except Exception:
            free_models = ["openrouter/free"]
    else:
        # dynamisch freie Modelle holen, sonst Default-Kette
        try:
            import requests
            r = requests.get("https://openrouter.ai/api/v1/models", timeout=5)
            free_models = [m["id"] for m in r.json().get("data", []) if ":free" in m["id"]][:3]
            free_models = ["openrouter/free"] + [f"openrouter/{m}" for m in free_models]
        except Exception:
            free_models = ["openrouter/free"]
    # Paid an erste Stelle (dedupliziert), Free danach als Notnagel
    return [paid] + [m for m in free_models if m != paid]


def _ollama_model() -> str:
    """Pick the best local Ollama model. Tries models in preference order."""
    import requests

    try:
        tags = requests.get("http://localhost:11434/api/tags", timeout=5).json()
        available = [m["name"] for m in tags.get("models", [])]
    except Exception:
        raise RuntimeError("Ollama not running. Start with: ollama serve")

    if not available:
        raise RuntimeError("No Ollama models found. Pull one: ollama pull llama3.1:8b")

    preferred = ["llama3.1:8b", "llama3:8b", "mistral", "gemma2", "qwen2.5:7b"]
    for pref in preferred:
        for avail in available:
            if pref in avail:
                log(f"Using Ollama model: {avail}")
                return f"ollama/{avail}"
    log(f"Using Ollama model: {available[0]}")
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
            return _call_litellm(prompt, max_tokens, model="anthropic/claude-sonnet-4-6",
                                 api_key=get_anthropic_key())
        if has_claude_cli():
            return call_claude_cli(prompt, max_tokens=max_tokens)
        raise RuntimeError(
            "No Claude access found. Set ANTHROPIC_API_KEY or install Claude Code."
        )
    if provider == "gemini":
        api_key = get_gemini_key()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        max_tokens = int(os.environ.get("GEMINI_MAX_TOKENS", max_tokens))
        # ponytail: manual fallback loop statt litellm-fallbacks (die reuse'n api_key falsch -> 401)
        gemini_model = f"gemini/{get_gemini_llm_model()}"
        try:
            return _call_litellm(prompt, max_tokens, model=gemini_model, api_key=api_key)
        except Exception as e:
            log(f"Gemini {gemini_model} failed: {e} -> trying OpenRouter fallbacks")
            # OpenRouter fallback chain manuell mit korrektem Key pro Modell
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
        return _call_litellm(prompt, max_tokens, model="openai/MiniMax-M2.7",
                             api_key=api_key,
                             api_base=os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1"),
                             temperature=1.0)
    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY") or load_config().get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        return _call_litellm(prompt, max_tokens, model="openai/gpt-4o-mini", api_key=api_key)
    if provider == "ollama":
        return _call_litellm(prompt, max_tokens, model=_ollama_model(), timeout=120)
    if provider == "litellm":
        return _call_litellm(prompt, max_tokens)
    raise ValueError(f"Unknown LLM provider: {provider}")


def _call_litellm(prompt: str, max_tokens: int, model: str | None = None,
                  fallbacks: list | None = None, api_key: str | None = None,
                  api_base: str | None = None, temperature: float = 0.7,
                  timeout: int | None = None) -> str:
    """Call any LLM provider via the litellm SDK.

    model defaults to LITELLM_MODEL (e.g. anthropic/claude-sonnet-4-20250514,
    azure/gpt-4o, bedrock/anthropic.claude-3-haiku, openai/gpt-4o).
    LiteLLM reads provider API keys from env vars automatically; api_key /
    api_base override per call. fallbacks fail over to the next model on error.

    See https://docs.litellm.ai/docs/providers for all supported models.
    """
    import litellm

    model = model or os.environ.get("LITELLM_MODEL", "openai/gpt-4o")
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
