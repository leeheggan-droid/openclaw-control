"""
OpenAI chat with per-user SQLite conversation memory.

Each user's conversation history is stored in the shared SQLite database
(``data/chat.db`` by default, overridable via ``CHAT_DB_PATH``).
Both the web app and the Telegram bot write to the same database so
memory is shared across channels for the same logical user.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from openai import OpenAI

# ── Configuration ─────────────────────────────────────────────────────────────

_DB_PATH = Path(os.environ.get("CHAT_DB_PATH", "data/chat.db"))
_SYSTEM_PROMPT = os.environ.get(
    "CHAT_SYSTEM_PROMPT",
    "You are a helpful AI assistant for the OpenClaw trading control system. "
    "Be concise, accurate, and professional.",
)
_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
_MAX_HISTORY = int(os.environ.get("CHAT_MAX_HISTORY", "40"))

_lock = threading.Lock()


# ── Database helpers ──────────────────────────────────────────────────────────


def _get_conn() -> sqlite3.Connection:
    """Open (and if needed, initialise) the chat database."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
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
    """)
    conn.commit()
    return conn


# Module-level shared connection (protected by _lock).
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _get_conn()
    return _conn


# ── Public API ────────────────────────────────────────────────────────────────


def get_history(user_id: str, limit: int = _MAX_HISTORY) -> list[dict]:
    """Return the last *limit* messages for *user_id*, oldest first."""
    with _lock:
        rows = _db().execute(
            "SELECT role, content FROM chat_messages "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def chat(user_id: str, message: str) -> str:
    """Send *message* as *user_id* and return the assistant reply.

    Conversation history is loaded from the database, the message is appended,
    the OpenAI completion is requested, and both the user message and the reply
    are persisted so future turns include them.
    """
    history = get_history(user_id)
    messages: list[dict] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})

    api_key = os.environ.get("OPENAI_API_KEY", "")
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=_MODEL,
        messages=messages,  # type: ignore[arg-type]
    )
    reply: str = response.choices[0].message.content or ""

    with _lock:
        db = _db()
        db.execute(
            "INSERT INTO chat_messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, "user", message),
        )
        db.execute(
            "INSERT INTO chat_messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, "assistant", reply),
        )
        db.commit()

    return reply


def clear_history(user_id: str) -> None:
    """Delete all stored messages for *user_id*."""
    with _lock:
        db = _db()
        db.execute("DELETE FROM chat_messages WHERE user_id = ?", (user_id,))
        db.commit()
