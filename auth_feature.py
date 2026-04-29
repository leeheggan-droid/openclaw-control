"""
Simple email+password authentication with bcrypt hashing and JWT sessions.

Users are stored in the shared SQLite database (same file as chat_feature).
On first boot, an admin account is created for AUTH_ADMIN_EMAIL using the
password in AUTH_ADMIN_DEFAULT_PASSWORD — change it immediately after the
first login.

Environment variables
---------------------
AUTH_ADMIN_EMAIL            (default: leeheggan@gmail.com)
AUTH_ADMIN_DEFAULT_PASSWORD (default: changeme123  — change on first login)
AUTH_SECRET_KEY             (REQUIRED in production — random string ≥ 32 chars)
JWT_ALGORITHM               (default: HS256)
JWT_EXPIRES_SECONDS         (overrides AUTH_TOKEN_EXPIRE_HOURS when set)
AUTH_TOKEN_EXPIRE_HOURS     (default: 168 = 7 days; ignored when JWT_EXPIRES_SECONDS is set)
CHAT_DB_PATH                (default: data/chat.db)

First-time setup
----------------
Run ``python init_db.py`` to create the database directory, tables, and admin
account before starting the web app for the first time.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import jwt
from dotenv import load_dotenv

load_dotenv("/etc/openclaw-control.env", override=True)
load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────

_log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_DB_PATH = Path(os.environ.get("CHAT_DB_PATH", "data/chat.db"))
_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "CHANGE-ME-IN-PRODUCTION-use-a-random-secret")
_ALGORITHM = os.environ.get("JWT_ALGORITHM", "HS256")
# JWT_EXPIRES_SECONDS takes precedence; AUTH_TOKEN_EXPIRE_HOURS is kept for backward compat
_jwt_expires_seconds = os.environ.get("JWT_EXPIRES_SECONDS")
if _jwt_expires_seconds:
    try:
        _TOKEN_EXPIRE_HOURS = int(_jwt_expires_seconds) // 3600
    except ValueError:
        _TOKEN_EXPIRE_HOURS = 168
else:
    try:
        _TOKEN_EXPIRE_HOURS = int(os.environ.get("AUTH_TOKEN_EXPIRE_HOURS", "168"))
    except ValueError:
        _TOKEN_EXPIRE_HOURS = 168

_ADMIN_EMAIL = os.environ.get("AUTH_ADMIN_EMAIL", "leeheggan@gmail.com")
_ADMIN_DEFAULT_PASSWORD = os.environ.get("AUTH_ADMIN_DEFAULT_PASSWORD", "changeme123")

_lock = threading.Lock()

# Warn loudly if the secret key is still the placeholder — all JWTs signed
# with this known string can be trivially forged.
_PLACEHOLDER_KEY = "CHANGE-ME-IN-PRODUCTION-use-a-random-secret"
if _SECRET_KEY == _PLACEHOLDER_KEY:
    warnings.warn(
        "AUTH_SECRET_KEY is set to the default placeholder value: all session tokens "
        "can be trivially forged. Set AUTH_SECRET_KEY to a long random string in "
        "/etc/openclaw-control.env before exposing this service to the internet.",
        stacklevel=1,
    )

# Ensure the data directory exists at import time so that any code that checks
# the directory (e.g. health probes, Docker volume mounts) never races with the
# first DB open.
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── Password helpers (direct bcrypt — no passlib required) ───────────────────


def _hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches *hashed*."""
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── Database helpers ──────────────────────────────────────────────────────────

_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        db_existed = _DB_PATH.exists()
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS auth_users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT    NOT NULL UNIQUE,
                password_hash TEXT    NOT NULL,
                created_at    TEXT    NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
        """)
        _conn.commit()
        if db_existed:
            _log.info("Auth DB opened: %s", _DB_PATH.resolve())
        else:
            _log.info("Auth DB created: %s", _DB_PATH.resolve())
    return _conn


def _ensure_admin() -> None:
    """Create the admin user with the default password if not present."""
    with _lock:
        row = _db().execute(
            "SELECT id FROM auth_users WHERE email = ?", (_ADMIN_EMAIL,)
        ).fetchone()
        if not row:
            hashed = _hash_password(_ADMIN_DEFAULT_PASSWORD)
            _db().execute(
                "INSERT INTO auth_users (email, password_hash) VALUES (?, ?)",
                (_ADMIN_EMAIL, hashed),
            )
            _db().commit()
            _log.info("Admin account created for %s", _ADMIN_EMAIL)


# ── Public API ────────────────────────────────────────────────────────────────


def authenticate(email: str, password: str) -> bool:
    """Return True if *email*+*password* match a stored account."""
    _ensure_admin()
    with _lock:
        row = _db().execute(
            "SELECT password_hash FROM auth_users WHERE email = ?", (email,)
        ).fetchone()
    if not row:
        return False
    return _verify_password(password, row["password_hash"])


def create_token(email: str) -> str:
    """Return a signed JWT for *email*."""
    expire = datetime.now(timezone.utc) + timedelta(hours=_TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": email, "exp": expire}, _SECRET_KEY, algorithm=_ALGORITHM)


def decode_token(token: str) -> str | None:
    """Return the email claim from *token*, or None if invalid/expired."""
    try:
        payload = jwt.decode(token, _SECRET_KEY, algorithms=[_ALGORITHM])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


def change_password(email: str, old_password: str, new_password: str) -> bool:
    """Change the password for *email*.  Returns False if *old_password* is wrong."""
    if not authenticate(email, old_password):
        return False
    hashed = _hash_password(new_password)
    with _lock:
        _db().execute(
            "UPDATE auth_users SET password_hash = ? WHERE email = ?",
            (hashed, email),
        )
        _db().commit()
    return True


def list_users() -> list[dict]:
    """Return all registered users as a list of dicts with *email* and *created_at*.

    Password hashes are never included.  Call ``_ensure_admin()`` first so the
    admin account is present even before the first explicit login.
    """
    _ensure_admin()
    with _lock:
        rows = _db().execute(
            "SELECT email, created_at FROM auth_users ORDER BY created_at ASC"
        ).fetchall()
    return [{"email": row["email"], "created_at": row["created_at"]} for row in rows]
