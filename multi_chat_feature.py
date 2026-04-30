"""
multi_chat_feature.py

Multi-provider chat with per-session SQLite memory, web search injection,
and support for OpenAI, Gemini, Anthropic, Groq, and Cerebras.

Provider routing:
  - openai, gemini, groq, cerebras — use the ``openai`` SDK with the
    appropriate ``base_url`` (all expose OpenAI-compatible endpoints).
  - anthropic — uses ``requests`` to call the Anthropic Messages API
    directly, since the ``anthropic`` SDK is not a project dependency.

Web search:
  - When ``web_search=True`` is passed to :func:`chat`, a Brave Search is
    performed first and the top results are prepended to the user message
    as context, so any provider can benefit from live web data.

Session management:
  - Each conversation is a *session* (UUID) owned by a user (email).
  - Sessions are stored in the ``chat_sessions`` table.
  - Messages reference their session via ``session_id``.
  - The first user message is used as the session title (≤ 50 chars).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import requests as _requests
from dotenv import load_dotenv
from openai import OpenAI

if TYPE_CHECKING:
    pass

load_dotenv("/etc/openclaw-control.env", override=True)
load_dotenv()

# ── Constants ──────────────────────────────────────────────────────────────────

_DB_PATH = Path(os.environ.get("CHAT_DB_PATH", "data/chat.db"))
_SYSTEM_PROMPT = os.environ.get(
    "CHAT_SYSTEM_PROMPT",
    "You are a helpful AI assistant for the OpenClaw trading control system. "
    "Be concise, accurate, and professional.",
)
_MAX_HISTORY = int(os.environ.get("CHAT_MAX_HISTORY", "40"))
_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_SEARCH_TIMEOUT = 10
_CHAT_TIMEOUT = 60

_lock = threading.Lock()

# ── Provider registry ──────────────────────────────────────────────────────────

PROVIDERS: dict[str, dict] = {
    "openai": {
        "name": "OpenAI",
        "base_url": None,
        "env_key": "OPENAI_API_KEY",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"],
        "default_model": "gpt-4o-mini",
        "client_type": "openai",
    },
    "gemini": {
        "name": "Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "env_key": "GEMINI_API_KEY",
        "models": ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"],
        "default_model": "gemini-2.0-flash",
        "client_type": "openai",
    },
    "anthropic": {
        "name": "Anthropic",
        "base_url": None,
        "env_key": "ANTHROPIC_API_KEY",
        "models": [
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
        ],
        "default_model": "claude-3-5-haiku-20241022",
        "client_type": "anthropic",
    },
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
        "models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
        ],
        "default_model": "llama-3.3-70b-versatile",
        "client_type": "openai",
    },
    "cerebras": {
        "name": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "env_key": "CEREBRAS_API_KEY",
        "models": ["llama3.1-70b", "llama3.1-8b"],
        "default_model": "llama3.1-70b",
        "client_type": "openai",
    },
}


def list_providers() -> dict[str, dict]:
    """Return the provider registry (name, models, default_model per provider)."""
    return {
        pid: {
            "name": info["name"],
            "models": info["models"],
            "default_model": info["default_model"],
        }
        for pid, info in PROVIDERS.items()
    }


# ── Database ───────────────────────────────────────────────────────────────────

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Create base tables (idempotent)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT    NOT NULL,
            role       TEXT    NOT NULL,
            content    TEXT    NOT NULL,
            created_at TEXT    NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS idx_chat_user
            ON chat_messages (user_id, id);

        CREATE TABLE IF NOT EXISTS chat_sessions (
            id         TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL,
            title      TEXT NOT NULL DEFAULT 'New chat',
            provider   TEXT NOT NULL DEFAULT 'openai',
            model      TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user
            ON chat_sessions (user_id, updated_at);
    """)
    # Migrate: add session_id column to chat_messages if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_messages)").fetchall()]
    if "session_id" not in cols:
        conn.execute("ALTER TABLE chat_messages ADD COLUMN session_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_session "
            "ON chat_messages (session_id, id)"
        )
    conn.commit()
    return conn


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _get_conn()
    return _conn


# ── Session helpers ────────────────────────────────────────────────────────────


def create_session(
    user_id: str,
    provider: str = "openai",
    model: str = "",
) -> dict:
    """Create a new session and return its metadata dict."""
    sid = str(uuid.uuid4())
    with _lock:
        _db().execute(
            "INSERT INTO chat_sessions (id, user_id, provider, model) VALUES (?, ?, ?, ?)",
            (sid, user_id, provider, model),
        )
        _db().commit()
    return {"id": sid, "title": "New chat", "provider": provider, "model": model}


def get_sessions(user_id: str) -> list[dict]:
    """Return all sessions for *user_id*, newest first."""
    with _lock:
        rows = _db().execute(
            "SELECT id, title, provider, model, created_at, updated_at "
            "FROM chat_sessions WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_session(session_id: str, user_id: str) -> bool:
    """Delete session and its messages. Returns False if session not found/owned."""
    with _lock:
        row = _db().execute(
            "SELECT id FROM chat_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
        if not row:
            return False
        _db().execute(
            "DELETE FROM chat_messages WHERE session_id = ?", (session_id,)
        )
        _db().execute(
            "DELETE FROM chat_sessions WHERE id = ?", (session_id,)
        )
        _db().commit()
    return True


def _update_session_title(session_id: str, first_message: str) -> None:
    title = first_message.strip()[:50]
    with _lock:
        _db().execute(
            "UPDATE chat_sessions SET title = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ?",
            (title, session_id),
        )
        _db().commit()


def _touch_session(session_id: str) -> None:
    with _lock:
        _db().execute(
            "UPDATE chat_sessions SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (session_id,),
        )
        _db().commit()


def get_session_history(session_id: str, limit: int = _MAX_HISTORY) -> list[dict]:
    """Return last *limit* messages for a session, oldest first."""
    with _lock:
        rows = _db().execute(
            "SELECT role, content FROM chat_messages "
            "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _save_messages(session_id: str, user_id: str, user_msg: str, reply: str) -> None:
    with _lock:
        db = _db()
        db.execute(
            "INSERT INTO chat_messages (user_id, session_id, role, content) VALUES (?, ?, ?, ?)",
            (user_id, session_id, "user", user_msg),
        )
        db.execute(
            "INSERT INTO chat_messages (user_id, session_id, role, content) VALUES (?, ?, ?, ?)",
            (user_id, session_id, "assistant", reply),
        )
        db.commit()


# ── Web search ─────────────────────────────────────────────────────────────────


def _brave_search(query: str, count: int = 5) -> str:
    """Return top Brave Search results as a plain-text block, or an error string.

    Returns an empty string only when there are genuinely no results.
    Returns a bracketed error string when the key is missing or the request fails,
    so callers can include the error as context for the LLM rather than silently
    dropping the web-search request.
    """
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        return (
            "[Web search unavailable: BRAVE_API_KEY is not configured. "
            "Add it to /etc/openclaw-control.env to enable web search.]"
        )
    query = query.strip()
    if not query:
        return ""
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    try:
        resp = _requests.get(
            _BRAVE_ENDPOINT,
            headers=headers,
            params={"q": query, "count": count},
            timeout=_SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except _requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        return (
            f"[Web search failed: HTTP {status} — check your Brave API key and quota.]"
        )
    except _requests.RequestException as exc:
        return f"[Web search failed: {type(exc).__name__} — check network connectivity.]"
    except (KeyError, ValueError):
        return "[Web search failed: unexpected response format from Brave API.]"

    results = (data.get("web") or {}).get("results") or []
    if not results:
        return ""

    lines: list[str] = [f"[Web search results for: {query}]"]
    for i, r in enumerate(results[:count], 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        desc = (r.get("description") or "").strip()
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   {url}")
        if desc:
            lines.append(f"   {desc}")
    lines.append("[End of web search results]\n")
    return "\n".join(lines)


# ── Provider call helpers ──────────────────────────────────────────────────────


def _call_openai_compatible(
    messages: list[dict],
    provider_id: str,
    model: str,
    info: dict,
) -> str:
    api_key = os.environ.get(info["env_key"], "")
    if not api_key:
        return f"❌ {info['name']} API key not configured. Set {info['env_key']} in your .env file."
    kwargs: dict = {"api_key": api_key}
    if info["base_url"]:
        kwargs["base_url"] = info["base_url"]
    client = OpenAI(**kwargs)
    response = client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        timeout=_CHAT_TIMEOUT,
    )
    return response.choices[0].message.content or ""


def _call_anthropic(
    messages: list[dict],
    model: str,
    system: str,
) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "❌ Anthropic API key not configured. Set ANTHROPIC_API_KEY in your .env file."
    # Anthropic expects system separate from messages
    anthropic_msgs = [m for m in messages if m["role"] != "system"]
    payload = {
        "model": model,
        "max_tokens": 4096,
        "system": system,
        "messages": anthropic_msgs,
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    try:
        resp = _requests.post(
            _ANTHROPIC_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=_CHAT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", [])
        for block in content:
            if block.get("type") == "text":
                return block["text"]
        return ""
    except _requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        return f"❌ Anthropic API error (HTTP {status}). Check your API key and quota."
    except _requests.RequestException as exc:
        return f"❌ Network error calling Anthropic: {type(exc).__name__}."
    except (KeyError, IndexError, ValueError):
        return "❌ Unexpected response format from Anthropic."


# ── Public API ─────────────────────────────────────────────────────────────────


def chat(
    user_id: str,
    message: str,
    session_id: str = "",
    provider: str = "openai",
    model: str = "",
    web_search: bool = False,
) -> str:
    """Send *message* and return the assistant reply.

    Parameters
    ----------
    user_id    : Authenticated user's email (used for ownership checks).
    message    : The user's message text.
    session_id : Session UUID. If empty, history is not persisted to a session.
    provider   : One of the keys in PROVIDERS (default: ``"openai"``).
    model      : Override the provider's default model.
    web_search : If True, prepend Brave Search results to the user message.
    """
    provider = (provider or "openai").lower()
    info = PROVIDERS.get(provider)
    if not info:
        supported = ", ".join(PROVIDERS.keys())
        return f"❌ Unknown provider '{provider}'. Supported: {supported}."

    chosen_model = (model or "").strip() or info["default_model"]

    # Optionally enrich with web search context
    user_content = message
    if web_search:
        search_results = _brave_search(message)
        if search_results:
            user_content = f"{search_results}\n{message}"

    # Build message history
    history = get_session_history(session_id) if session_id else []
    messages: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_content})

    # Route to provider
    if info["client_type"] == "anthropic":
        reply = _call_anthropic(messages, chosen_model, _SYSTEM_PROMPT)
    else:
        reply = _call_openai_compatible(messages, provider, chosen_model, info)

    # Persist
    if session_id and not reply.startswith("❌"):
        _save_messages(session_id, user_id, message, reply)
        # Auto-set session title from first message
        if not history:
            _update_session_title(session_id, message)
        else:
            _touch_session(session_id)

    return reply


def clear_session(session_id: str, user_id: str) -> None:
    """Delete all messages in *session_id* (but keep the session record)."""
    with _lock:
        # Verify ownership before deleting
        row = _db().execute(
            "SELECT id FROM chat_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
        if row:
            _db().execute(
                "DELETE FROM chat_messages WHERE session_id = ?", (session_id,)
            )
            _db().commit()
