"""openclaw_control/projects.py — Long-term project memory store."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Resolve DB path: sit alongside the trade-log DB when configured, else use cwd.
_env_db = os.environ.get("OPENCLAW_TRADE_LOG_DB", "")
if _env_db:
    _DB_PATH = Path(_env_db).parent / ".openclaw_projects.sqlite"
else:
    _DB_PATH = Path.cwd() / ".openclaw_projects.sqlite"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS projects (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        notes      TEXT NOT NULL DEFAULT '',
        repo       TEXT NOT NULL DEFAULT '',
        tags       TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    c.commit()
    return c


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_projects() -> list[dict]:
    """Return all projects ordered by most-recently updated."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM projects ORDER BY updated_at DESC"
        )]


def create_project(name: str, notes: str = "", repo: str = "", tags: str = "") -> dict:
    """Insert a new project and return the created row."""
    now = _now()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO projects (name, notes, repo, tags, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (name, notes, repo, tags, now, now),
        )
        c.commit()
        row = c.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)


_ALLOWED_COLS = frozenset({"name", "notes", "repo", "tags"})


def update_project(project_id: int, **kwargs: str) -> dict | None:
    """Update allowed fields on a project; return the updated row or None if not found."""
    updates = {k: v for k, v in kwargs.items() if k in _ALLOWED_COLS}
    if not updates:
        return None
    updates["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [project_id]
    with _conn() as c:
        c.execute(f"UPDATE projects SET {cols} WHERE id=?", vals)  # noqa: S608
        c.commit()
        row = c.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return dict(row) if row else None


def delete_project(project_id: int) -> bool:
    """Delete a project by id; returns True if a row was removed."""
    with _conn() as c:
        cur = c.execute("DELETE FROM projects WHERE id=?", (project_id,))
        c.commit()
        return cur.rowcount > 0
