#!/usr/bin/env python3
"""
init_db.py — First-time database and admin account initialisation for
OpenClaw Control.

Run this script once before starting the web app to make sure the database
directory exists, the schema is created, and the admin user is present.

Usage
-----
    python init_db.py

Environment variables (all optional — defaults are used if not set)
-------------------------------------------------------------------
    CHAT_DB_PATH                Path to the SQLite database  (default: data/chat.db)
    AUTH_ADMIN_EMAIL            Admin account email          (default: leeheggan@gmail.com)
    AUTH_ADMIN_DEFAULT_PASSWORD Admin account password       (default: changeme123)

Troubleshooting
---------------
- "ModuleNotFoundError: No module named 'bcrypt'":
      pip install bcrypt
- "PermissionError" on the data/ directory:
      Make sure the user running this script can write to the project directory.
      sudo chown -R $USER data/   # or wherever CHAT_DB_PATH points
- Admin already exists:
      The script is safe to re-run; it will not overwrite an existing admin.
- To verify the admin was created after running this script:
      sqlite3 data/chat.db "SELECT email, created_at FROM auth_users;"
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(message)s",
)
_log = logging.getLogger(__name__)

# ── Step 1: verify bcrypt is usable ──────────────────────────────────────────
_log.info("Checking bcrypt…")
try:
    import bcrypt as _bcrypt_mod

    _test_hash = _bcrypt_mod.hashpw(b"probe", _bcrypt_mod.gensalt())
    assert _bcrypt_mod.checkpw(b"probe", _test_hash), "checkpw sanity failed"
    _log.info("bcrypt OK  (version: %s)", getattr(_bcrypt_mod, "__version__", "unknown"))
except Exception as exc:  # noqa: BLE001
    _log.error(
        "bcrypt is broken or not installed: %s\n"
        "Fix:  pip install --upgrade bcrypt",
        exc,
    )
    sys.exit(1)

# ── Step 2: ensure the data directory exists ─────────────────────────────────
_db_path = Path(os.environ.get("CHAT_DB_PATH", "data/chat.db"))
_log.info("Database path: %s", _db_path.resolve())

try:
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    _log.info("data/ directory ready: %s", _db_path.parent.resolve())
except OSError as exc:
    _log.error("Cannot create data directory %s: %s", _db_path.parent, exc)
    sys.exit(1)

# ── Step 3: import auth_feature and create tables + admin ────────────────────
try:
    import auth_feature  # noqa: E402 — intentional late import (env vars must be set first)
except Exception as exc:  # noqa: BLE001
    _log.error("Failed to import auth_feature: %s", exc)
    sys.exit(1)

try:
    auth_feature._ensure_admin()
except Exception as exc:  # noqa: BLE001
    _log.error("Failed to create admin account: %s", exc)
    sys.exit(1)

# ── Step 4: confirm success ───────────────────────────────────────────────────
admin_email = os.environ.get("AUTH_ADMIN_EMAIL", "leeheggan@gmail.com")
_log.info("✓ Admin account ready for %s", admin_email)
_log.info("✓ Database initialised at %s", _db_path.resolve())
print()
print("First-time setup complete.")
print(f"  DB path    : {_db_path.resolve()}")
print(f"  Admin email: {admin_email}")
print()
print("To verify:")
print(f"  sqlite3 {_db_path} \"SELECT email, created_at FROM auth_users;\"")
print()
print("Change the admin password on first login via /auth/change-password")
print("or set AUTH_ADMIN_DEFAULT_PASSWORD before running this script.")
