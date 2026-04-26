"""
cheap_chat_feature.py

Lightweight multi-turn chat using cheap inference providers (Groq, Mistral, Cerebras).
All three expose an OpenAI-compatible /chat/completions endpoint, so the implementation
is identical aside from the base URL, default model, and API key.

This module is intentionally self-contained and does NOT use the OpenAI Agents SDK so
that it cannot interfere with the existing agent / autopilot loop.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, dict] = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "env_key": "GROQ_API_KEY",
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "default_model": "mistral-small-latest",
        "env_key": "MISTRAL_API_KEY",
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "default_model": "llama3.1-70b",
        "env_key": "CEREBRAS_API_KEY",
    },
}

_TIMEOUT = 30  # seconds


def _get_api_key(provider: str) -> str:
    info = _PROVIDERS.get(provider)
    if not info:
        return ""
    # Prefer the value already stored in the runtime environment (set by config.py / .env)
    return os.environ.get(info["env_key"], "")


def cheap_chat(
    message: str,
    provider: str = "groq",
    history: list[dict] | None = None,
    model: str = "",
    system: str = "You are a helpful assistant.",
    max_tokens: int = 1024,
) -> str:
    """
    Send *message* to the chosen provider and return the assistant reply.

    Parameters
    ----------
    message   : The user's latest message.
    provider  : One of "groq", "mistral", "cerebras".
    history   : Optional list of prior turns, each ``{"role": ..., "content": ...}``.
                Only "user" and "assistant" roles are forwarded (system is injected separately).
    model     : Override the default model for the provider.
    system    : System prompt injected at position 0.
    max_tokens: Maximum tokens in the completion.

    Returns a plain-text reply string.  On error, returns a string beginning with "❌".
    """
    provider = (provider or "groq").lower()
    info = _PROVIDERS.get(provider)
    if not info:
        supported = ", ".join(_PROVIDERS.keys())
        return f"❌ Unknown provider {provider!r}. Supported: {supported}."

    api_key = _get_api_key(provider)
    if not api_key:
        env_var = info["env_key"]
        return (
            f"❌ {provider.capitalize()} API key not configured. "
            f"Set {env_var} in your .env file."
        )

    chosen_model = (model or "").strip() or info["default_model"]
    base_url = info["base_url"]

    # Build messages array
    messages: list[dict] = [{"role": "system", "content": system}]
    for turn in (history or []):
        role = turn.get("role", "")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": chosen_model,
        "messages": messages,
        "max_tokens": max_tokens,
    }

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        # Avoid leaking the response body which may contain the API key in error messages
        return f"❌ {provider.capitalize()} API error (HTTP {status}). Check your API key and quota."
    except requests.RequestException as exc:
        return f"❌ Network error calling {provider.capitalize()}: {type(exc).__name__}."
    except (KeyError, IndexError, ValueError):
        return f"❌ Unexpected response format from {provider.capitalize()}."
