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
AUTH_TOKEN_EXPIRE_HOURS     (default: 168 = 7 days)
CHAT_DB_PATH                (default: data/chat.db)
"""

from __future__ import annotations

import os
import sqlite3
import threading
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from passlib.context import CryptContext

# ── Configuration ─────────────────────────────────────────────────────────────

_DB_PATH = Path(os.environ.get("CHAT_DB_PATH", "data/chat.db"))
_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", "CHANGE-ME-IN-PRODUCTION-use-a-random-secret")
_ALGORITHM = "HS256"
_TOKEN_EXPIRE_HOURS = int(os.environ.get("AUTH_TOKEN_EXPIRE_HOURS", "168"))

_ADMIN_EMAIL = os.environ.get("AUTH_ADMIN_EMAIL", "leeheggan@gmail.com")
_ADMIN_DEFAULT_PASSWORD = os.environ.get("AUTH_ADMIN_DEFAULT_PASSWORD", "changeme123")

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
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

# ── Database helpers ──────────────────────────────────────────────────────────

_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
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
    return _conn


def _ensure_admin() -> None:
    """Create the admin user with the default password if not present."""
    with _lock:
        row = _db().execute(
            "SELECT id FROM auth_users WHERE email = ?", (_ADMIN_EMAIL,)
        ).fetchone()
        if not row:
            hashed = _pwd_ctx.hash(_ADMIN_DEFAULT_PASSWORD)
            _db().execute(
                "INSERT INTO auth_users (email, password_hash) VALUES (?, ?)",
                (_ADMIN_EMAIL, hashed),
            )
            _db().commit()


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
    return _pwd_ctx.verify(password, row["password_hash"])


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
    hashed = _pwd_ctx.hash(new_password)
    with _lock:
        _db().execute(
            "UPDATE auth_users SET password_hash = ? WHERE email = ?",
            (hashed, email),
        )
        _db().commit()
    return True
