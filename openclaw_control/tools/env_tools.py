"""openclaw_control/tools/env_tools.py

Agent tool for inspecting the runtime environment.

Provides ``env_diagnostics`` — a function_tool the main agent can call to
check which environment variables are currently set in ``os.environ``.
Because it reads ``os.environ`` at *call time* (not at import time), it
always reflects the live state of the process environment and is not
affected by import-ordering issues.
"""

from __future__ import annotations

import os

from agents import function_tool

# Canonical list of environment variables relevant to OpenClaw Control.
_KNOWN_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENCLAW_SSH_HOST",
    "OPENCLAW_SSH_READONLY_HOST",
    "OPENCLAW_REPO_DIR",
    "GITHUB_TOKEN",
    "PORT",
    "AUTH_SECRET_KEY",
    "JWT_ALGORITHM",
    "JWT_EXPIRES_SECONDS",
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_2",
    "BRAVE_API_KEY",
    "OPENAI_API_KEY",
    "CEREBRAS_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "TELEGRAM_TOKEN",
    "AGENT_ID",
    "ENV_ID",
    "KRAKEN_API_KEY",
    "KRAKEN_SECRET_KEY",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "AUTH_ADMIN_DEFAULT_PASSWORD",
    "AUTH_ADMIN_EMAIL",
)


@function_tool
def env_diagnostics() -> str:
    """Check which environment variables are currently set in the runtime process.

    Reads os.environ at call time so the result always reflects the live state —
    including any values loaded by load_dotenv at startup.

    Use this when the user asks about:
    - Which API keys or env vars are configured
    - Whether a specific service key is set
    - System environment diagnostics / self-test

    Returns a plain-text report listing each known key as SET (with character
    count) or NOT SET.
    """
    lines: list[str] = ["Runtime environment variable status:"]
    for key in _KNOWN_KEYS:
        value = os.environ.get(key, "")
        if value:
            lines.append(f"  {key}: SET")
        else:
            lines.append(f"  {key}: NOT SET")
    return "\n".join(lines)
