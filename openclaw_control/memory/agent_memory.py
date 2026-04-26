"""openclaw_control/memory/agent_memory.py

Persistent, evidence-gated memory store for P&L, Quant, and COO agents.

Design principles (non-negotiable):
- Memory is ONLY updated when evidence exists (Vibe/SSH snapshot, trade probes, logs).
- Memory is automatically invalidated when the environment fingerprint changes
  (container restart, git HEAD change, SSH target change).
- Only derived summaries and flags are stored — never secrets, never raw full logs,
  never entire trade dumps.
- All reads and writes are thread-safe.

Schema
------
  memory_snapshots(
      agent      TEXT PRIMARY KEY,
      snapshot_json TEXT,      -- JSON blob of derived summaries & flags
      updated_at TEXT,         -- ISO-8601 UTC
      fingerprint TEXT         -- env hash; mismatch triggers auto-invalidation
  )

  memory_events(
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      agent      TEXT NOT NULL,
      ts         TEXT NOT NULL,  -- ISO-8601 UTC
      kind       TEXT NOT NULL,  -- e.g. "update", "invalidate", "probe_result"
      payload_json TEXT          -- JSON blob (max ~4 KB)
  )

Public API
----------
  compute_fingerprint()           -> str
  load_snapshot(agent)            -> dict   (empty dict if none / stale)
  save_snapshot(agent, data, fp)  -> None
  append_event(agent, kind, payload) -> None
  invalidate(agent, reason)       -> None
  get_events(agent, limit)        -> list[dict]
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone as _tz
from pathlib import Path
from typing import Any

from openclaw_control.config import settings

# ---------------------------------------------------------------------------
# Database location
# ---------------------------------------------------------------------------

_DB_PATH = Path(__file__).parent.parent.parent / ".openclaw_memory.sqlite"

# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(_tz.utc).replace(microsecond=0).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memory_snapshots (
            agent         TEXT PRIMARY KEY,
            snapshot_json TEXT NOT NULL DEFAULT '{}',
            updated_at    TEXT NOT NULL,
            fingerprint   TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS memory_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            agent        TEXT NOT NULL,
            ts           TEXT NOT NULL,
            kind         TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_mem_events_agent
            ON memory_events(agent, id DESC);
    """)
    conn.commit()


# Initialise schema on first import (safe to call repeatedly).
with _LOCK:
    _conn: sqlite3.Connection = _connect()
    _ensure_schema(_conn)


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

_MAX_FINGERPRINT_PAYLOAD = 2000   # chars; truncate raw SSH output before hashing


def compute_fingerprint(container_start_times: str = "", git_head: str = "") -> str:
    """Compute a short hex fingerprint of the current environment.

    The fingerprint includes:
    - ssh_host (from config)
    - repo_dir (from config)
    - container start times (caller supplies from ``docker inspect``)
    - git HEAD hash on VPS (caller supplies)

    When any of these change the old snapshot is automatically invalidated.
    ``container_start_times`` and ``git_head`` are optional — callers that
    cannot gather SSH data still get a stable fingerprint that reflects the
    local config.
    """
    raw = "|".join([
        settings.ssh_host or "",
        settings.repo_dir or "",
        container_start_times[:_MAX_FINGERPRINT_PAYLOAD],
        git_head[:40],
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_snapshot(agent: str) -> dict[str, Any]:
    """Return the stored snapshot dict for *agent*, or {} if absent / stale.

    The snapshot is returned without fingerprint validation here; callers that
    want staleness detection should compare ``load_snapshot`` against
    ``compute_fingerprint`` themselves (see ``service.py``).
    """
    with _LOCK:
        row = _conn.execute(
            "SELECT snapshot_json, fingerprint FROM memory_snapshots WHERE agent = ?",
            (agent,),
        ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row["snapshot_json"]) or {}
    except Exception:
        return {}


def load_snapshot_with_fingerprint(agent: str) -> tuple[dict[str, Any], str]:
    """Return (snapshot_dict, fingerprint) for *agent*.

    Both values are empty / '' if no snapshot exists.
    """
    with _LOCK:
        row = _conn.execute(
            "SELECT snapshot_json, fingerprint FROM memory_snapshots WHERE agent = ?",
            (agent,),
        ).fetchone()
    if not row:
        return {}, ""
    try:
        return json.loads(row["snapshot_json"]) or {}, row["fingerprint"] or ""
    except Exception:
        return {}, ""


def save_snapshot(agent: str, data: dict[str, Any], fingerprint: str) -> None:
    """Persist *data* as the memory snapshot for *agent*.

    The caller is responsible for ensuring *data* contains only derived
    summaries and flags — never raw logs, never secrets.
    """
    now = _now_iso()
    payload = json.dumps(data, ensure_ascii=False)
    with _LOCK:
        _conn.execute(
            """
            INSERT INTO memory_snapshots (agent, snapshot_json, updated_at, fingerprint)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(agent) DO UPDATE SET
                snapshot_json = excluded.snapshot_json,
                updated_at    = excluded.updated_at,
                fingerprint   = excluded.fingerprint
            """,
            (agent, payload, now, fingerprint),
        )
        _conn.commit()
    append_event(agent, "update", {"fingerprint": fingerprint, "keys": list(data.keys())})


def append_event(agent: str, kind: str, payload: dict[str, Any]) -> None:
    """Record a memory event (update, invalidate, probe_result, etc.)."""
    now = _now_iso()
    # Hard cap payload to prevent storing raw log dumps
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) > 4096:
        raw = json.dumps({"truncated": True, "preview": raw[:200]})
    with _LOCK:
        _conn.execute(
            "INSERT INTO memory_events (agent, ts, kind, payload_json) VALUES (?, ?, ?, ?)",
            (agent, now, kind, raw),
        )
        _conn.commit()


def invalidate(agent: str, reason: str) -> None:
    """Clear the snapshot for *agent* and record why it was invalidated."""
    now = _now_iso()
    with _LOCK:
        _conn.execute(
            """
            INSERT INTO memory_snapshots (agent, snapshot_json, updated_at, fingerprint)
            VALUES (?, '{}', ?, '')
            ON CONFLICT(agent) DO UPDATE SET
                snapshot_json = '{}',
                updated_at    = excluded.updated_at,
                fingerprint   = ''
            """,
            (agent, now),
        )
        _conn.commit()
    append_event(agent, "invalidate", {"reason": reason[:500]})


def get_events(agent: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent *limit* events for *agent*, newest first."""
    with _LOCK:
        rows = _conn.execute(
            "SELECT id, ts, kind, payload_json FROM memory_events "
            "WHERE agent = ? ORDER BY id DESC LIMIT ?",
            (agent, limit),
        ).fetchall()
    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            payload = {}
        result.append({
            "id": row["id"],
            "ts": row["ts"],
            "kind": row["kind"],
            "payload": payload,
        })
    return result
