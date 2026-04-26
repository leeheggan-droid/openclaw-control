"""openclaw_control/trade_log.py

Persistent on-disk logging for trade executions and P&L snapshots.

All data is stored in a SQLite database that survives container restarts.
A background scheduler thread:
  - Persists hourly P&L snapshots via the SSH pnl_snapshot probe.
  - Checks for trade inactivity and emits a log warning + optional webhook
    alert when no new trade has been recorded within the configured window.

Schema
------
  trade_executions(
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      ts         TEXT NOT NULL,      -- ISO-8601 UTC
      symbol     TEXT NOT NULL,
      side       TEXT NOT NULL,      -- 'buy' | 'sell'
      size       REAL NOT NULL,
      fill_price REAL NOT NULL,
      trade_id   TEXT NOT NULL DEFAULT '',
      source     TEXT NOT NULL DEFAULT ''  -- e.g. 'api', 'ssh_probe'
  )

  pnl_snapshots(
      id             INTEGER PRIMARY KEY AUTOINCREMENT,
      ts             TEXT NOT NULL,
      total_pnl      REAL,
      equity         REAL,
      drawdown       REAL,
      realised_pnl   REAL,
      unrealised_pnl REAL,
      sharpe_ratio   REAL,
      source         TEXT NOT NULL DEFAULT ''
  )

Public API
----------
  log_trade(ts, symbol, side, size, fill_price, trade_id, source) -> int
  log_pnl_snapshot(ts, total_pnl, equity, drawdown,
                   realised_pnl, unrealised_pnl, sharpe_ratio, source) -> int
  get_recent_trades(limit)   -> list[dict]
  get_recent_pnl(limit)      -> list[dict]
  get_last_trade_time()      -> datetime | None
  get_inactivity_status()    -> dict
  start_scheduler()          -> None   (idempotent; call once at app startup)
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone as _tz, timedelta
from pathlib import Path
from typing import Any

import requests as _requests

from openclaw_control.config import settings

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database location
# ---------------------------------------------------------------------------

_DB_PATH = Path(
    os.environ.get("OPENCLAW_TRADE_LOG_DB", "")
    or Path.cwd() / ".openclaw_trades.sqlite"
)

# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(_tz.utc).replace(microsecond=0).isoformat()


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (public alias)."""
    return _now_iso()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_executions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT NOT NULL,
            symbol     TEXT NOT NULL,
            side       TEXT NOT NULL,
            size       REAL NOT NULL,
            fill_price REAL NOT NULL,
            trade_id   TEXT NOT NULL DEFAULT '',
            source     TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_trade_ts
            ON trade_executions(ts DESC);

        CREATE TABLE IF NOT EXISTS pnl_snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             TEXT NOT NULL,
            total_pnl      REAL,
            equity         REAL,
            drawdown       REAL,
            realised_pnl   REAL,
            unrealised_pnl REAL,
            sharpe_ratio   REAL,
            source         TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_pnl_ts
            ON pnl_snapshots(ts DESC);
    """)
    conn.commit()


# Initialise schema on first import.
with _LOCK:
    _conn: sqlite3.Connection = _connect()
    _ensure_schema(_conn)


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------


def log_trade(
    ts: str,
    symbol: str,
    side: str,
    size: float,
    fill_price: float,
    trade_id: str = "",
    source: str = "api",
) -> int:
    """Insert a trade execution record and return the new row id."""
    with _LOCK:
        cur = _conn.execute(
            """
            INSERT INTO trade_executions (ts, symbol, side, size, fill_price, trade_id, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, symbol, side, float(size), float(fill_price), trade_id or "", source or "api"),
        )
        _conn.commit()
        return cur.lastrowid or 0


def log_pnl_snapshot(
    ts: str,
    total_pnl: float | None = None,
    equity: float | None = None,
    drawdown: float | None = None,
    realised_pnl: float | None = None,
    unrealised_pnl: float | None = None,
    sharpe_ratio: float | None = None,
    source: str = "api",
) -> int:
    """Insert a P&L snapshot record and return the new row id."""
    with _LOCK:
        cur = _conn.execute(
            """
            INSERT INTO pnl_snapshots
                (ts, total_pnl, equity, drawdown, realised_pnl, unrealised_pnl, sharpe_ratio, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, total_pnl, equity, drawdown, realised_pnl, unrealised_pnl, sharpe_ratio, source or "api"),
        )
        _conn.commit()
        return cur.lastrowid or 0


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def get_recent_trades(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent *limit* trade executions, newest first."""
    with _LOCK:
        rows = _conn.execute(
            "SELECT id, ts, symbol, side, size, fill_price, trade_id, source "
            "FROM trade_executions ORDER BY id DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_pnl(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent *limit* P&L snapshots, newest first."""
    with _LOCK:
        rows = _conn.execute(
            "SELECT id, ts, total_pnl, equity, drawdown, realised_pnl, "
            "unrealised_pnl, sharpe_ratio, source "
            "FROM pnl_snapshots ORDER BY id DESC LIMIT ?",
            (max(1, limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_last_trade_time() -> datetime | None:
    """Return the UTC datetime of the most recent trade, or None if no trades logged."""
    with _LOCK:
        row = _conn.execute(
            "SELECT ts FROM trade_executions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row["ts"])
    except Exception:
        return None


def get_inactivity_status() -> dict[str, Any]:
    """Return a dict describing the current trade inactivity state."""
    window_hours: int = settings.trade_inactivity_hours
    last_ts = get_last_trade_time()
    now = datetime.now(_tz.utc)

    if last_ts is None:
        # No trades have ever been recorded
        inactive_for_hours: float | None = None
        is_inactive = True
        last_trade_iso = None
    else:
        # Make both timezone-aware for comparison
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=_tz.utc)
        delta = now - last_ts
        inactive_for_hours = delta.total_seconds() / 3600.0
        is_inactive = inactive_for_hours >= window_hours
        last_trade_iso = last_ts.isoformat()

    return {
        "last_trade_ts": last_trade_iso,
        "window_hours": window_hours,
        "inactive_for_hours": round(inactive_for_hours, 2) if inactive_for_hours is not None else None,
        "is_inactive": is_inactive,
        "checked_at": now.replace(microsecond=0).isoformat(),
    }


# ---------------------------------------------------------------------------
# Background scheduler
# ---------------------------------------------------------------------------

_SCHEDULER_STARTED = False
_SCHEDULER_LOCK = threading.Lock()

# Interval constants (seconds)
_INACTIVITY_CHECK_INTERVAL = 30 * 60      # 30 minutes
_PNL_SNAPSHOT_INTERVAL = 60 * 60         # 1 hour

# Track last alert time to avoid flooding
_last_alert_sent: datetime | None = None
_ALERT_COOLDOWN_HOURS = 1


def _send_inactivity_alert(status: dict[str, Any]) -> None:
    """Log a warning and optionally POST to the configured webhook URL."""
    inactive_h = status.get("inactive_for_hours")
    window_h = status.get("window_hours")
    last = status.get("last_trade_ts") or "never"

    inactive_str = f"{inactive_h:.1f}h" if inactive_h is not None else "unknown (no trades ever recorded)"
    msg = (
        f"[OpenClaw] Trade inactivity alert: no trades recorded for "
        f"{inactive_str} (window={window_h}h). Last trade: {last}."
    )
    _logger.warning(msg)

    webhook_url = settings.alert_webhook_url
    if not webhook_url:
        return

    payload = {
        "text": msg,
        "event": "trade_inactivity",
        "details": status,
    }
    try:
        resp = _requests.post(webhook_url, json=payload, timeout=10)
        if not resp.ok:
            _logger.warning(
                "Trade inactivity webhook returned %s: %s",
                resp.status_code,
                resp.text[:200],
            )
    except Exception as exc:
        _logger.warning("Trade inactivity webhook failed: %s", exc)


def _maybe_take_pnl_snapshot(pnl_probe_fn=None) -> None:
    """Run the P&L snapshot probe and persist the result.

    ``pnl_probe_fn`` is an optional zero-argument callable that returns the
    raw P&L snapshot string (e.g. from ``service.run_vibe_report``).  When
    not supplied the snapshot is skipped (SSH not available at this layer).
    This avoids a circular import between trade_log and service.
    """
    if pnl_probe_fn is None:
        return
    try:
        output = pnl_probe_fn()
        if output and len(output) > 50:
            log_pnl_snapshot(
                ts=_now_iso(),
                source="scheduler_ssh_probe",
                # Numeric fields are not parsed from raw log text here;
                # store None so the row records the snapshot timestamp.
            )
            _logger.info("Hourly P&L snapshot persisted (%d chars)", len(output))
    except Exception as exc:
        _logger.debug("Hourly P&L snapshot skipped: %s", exc)


def _scheduler_loop(pnl_probe_fn=None) -> None:
    """Background thread: inactivity checks every 30 min, P&L snapshots every hour."""
    global _last_alert_sent

    next_pnl_snapshot = time.monotonic() + _PNL_SNAPSHOT_INTERVAL

    while True:
        try:
            # ── Inactivity check ──────────────────────────────────────────────
            status = get_inactivity_status()
            if status["is_inactive"]:
                now = datetime.now(_tz.utc)
                cooldown = timedelta(hours=_ALERT_COOLDOWN_HOURS)
                with _SCHEDULER_LOCK:
                    should_alert = (
                        _last_alert_sent is None or (now - _last_alert_sent) >= cooldown
                    )
                    if should_alert:
                        _last_alert_sent = now
                if should_alert:
                    _send_inactivity_alert(status)

            # ── Hourly P&L snapshot ───────────────────────────────────────────
            if time.monotonic() >= next_pnl_snapshot:
                _maybe_take_pnl_snapshot(pnl_probe_fn)
                next_pnl_snapshot = time.monotonic() + _PNL_SNAPSHOT_INTERVAL

        except Exception as exc:
            _logger.debug("Scheduler loop error (non-fatal): %s", exc)

        time.sleep(_INACTIVITY_CHECK_INTERVAL)


def start_scheduler(pnl_probe_fn=None) -> None:
    """Start the background scheduler thread (idempotent — safe to call multiple times).

    ``pnl_probe_fn`` is an optional zero-argument callable that returns the raw
    P&L snapshot string for the hourly probe (e.g. ``lambda: run_vibe_report("pnl_snapshot")``).
    If omitted, hourly P&L snapshots are skipped (inactivity alerting still works).
    """
    global _SCHEDULER_STARTED
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return
        _SCHEDULER_STARTED = True

    t = threading.Thread(
        target=_scheduler_loop,
        args=(pnl_probe_fn,),
        daemon=True,
        name="trade-log-scheduler",
    )
    t.start()
    _logger.info("Trade log scheduler started (inactivity window: %dh)", settings.trade_inactivity_hours)
