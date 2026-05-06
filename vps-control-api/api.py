"""
VPS Control API
---------------
FastAPI service exposing status, logs, restart, and deploy endpoints for the
systemd-managed bot services running on srv1501082.

Auth:  Authorization: Bearer <VPS_CONTROL_API_KEY>
Port:  8765 (HTTP — TLS termination via nginx is a future step)

Run:
    uvicorn api:app --host 0.0.0.0 --port 8765
"""

import os
import secrets
import subprocess
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.security.api_key import APIKeyHeader

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_raw_key = os.environ.get("VPS_CONTROL_API_KEY", "")
if not _raw_key:
    raise RuntimeError("VPS_CONTROL_API_KEY environment variable is required but not set")

API_KEY: str = _raw_key

# Map unit name → git repo root for the /deploy endpoint.
# Run `systemctl cat <unit>` on the VPS to confirm the working directories.
_DEPLOY_PATHS: dict[str, str] = {
    "openclaw-agent.service": "/opt/openclaw-agent",
    "openclaw-crypto.service": "/home/jacks/openclaw-crypto",
    # alpaca_orb_bite_bot — confirm path via `systemctl cat alpaca_orb_bite_bot.service`
    # "alpaca_orb_bite_bot.service": "/home/jacks/alpaca_orb_bite_bot",
}

# ---------------------------------------------------------------------------
# Pre-built command tables — all subprocess args are Python literals.
# User input is ONLY used as a dict key; it never flows into these lists.
# ---------------------------------------------------------------------------

_STATUS_CMDS: dict[str, list[str]] = {
    "openclaw-agent.service":       ["systemctl", "is-active", "openclaw-agent.service"],
    "openclaw-crypto.service":      ["systemctl", "is-active", "openclaw-crypto.service"],
    "openclaw-vibe-gateway.service":["systemctl", "is-active", "openclaw-vibe-gateway.service"],
    "alpaca_orb_bite_bot.service":  ["systemctl", "is-active", "alpaca_orb_bite_bot.service"],
    "linkedin-news.timer":          ["systemctl", "is-active", "linkedin-news.timer"],
    "linkedin-news.service":        ["systemctl", "is-active", "linkedin-news.service"],
}

_LOGS_CMDS: dict[str, list[str]] = {
    "openclaw-agent.service":       ["sudo", "journalctl", "-u", "openclaw-agent.service",       "--no-pager", "--output=short-iso"],
    "openclaw-crypto.service":      ["sudo", "journalctl", "-u", "openclaw-crypto.service",      "--no-pager", "--output=short-iso"],
    "openclaw-vibe-gateway.service":["sudo", "journalctl", "-u", "openclaw-vibe-gateway.service","--no-pager", "--output=short-iso"],
    "alpaca_orb_bite_bot.service":  ["sudo", "journalctl", "-u", "alpaca_orb_bite_bot.service",  "--no-pager", "--output=short-iso"],
    "linkedin-news.timer":          ["sudo", "journalctl", "-u", "linkedin-news.timer",          "--no-pager", "--output=short-iso"],
    "linkedin-news.service":        ["sudo", "journalctl", "-u", "linkedin-news.service",        "--no-pager", "--output=short-iso"],
}

_RESTART_CMDS: dict[str, list[str]] = {
    "openclaw-agent.service":       ["sudo", "systemctl", "restart", "openclaw-agent.service"],
    "openclaw-crypto.service":      ["sudo", "systemctl", "restart", "openclaw-crypto.service"],
    "openclaw-vibe-gateway.service":["sudo", "systemctl", "restart", "openclaw-vibe-gateway.service"],
    "alpaca_orb_bite_bot.service":  ["sudo", "systemctl", "restart", "alpaca_orb_bite_bot.service"],
    "linkedin-news.timer":          ["sudo", "systemctl", "restart", "linkedin-news.timer"],
    "linkedin-news.service":        ["sudo", "systemctl", "restart", "linkedin-news.service"],
}

_DEPLOY_CMDS: dict[str, list[str]] = {
    svc: ["git", "-C", path, "pull"]
    for svc, path in _DEPLOY_PATHS.items()
}

ALLOWED_SERVICES: set[str] = set(_STATUS_CMDS)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="VPS Control API", version="1.0.0")

_api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(api_key: Optional[str]) -> None:
    token = (api_key or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token.encode(), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _validate_service(service: str) -> None:
    if service not in ALLOWED_SERVICES:
        raise HTTPException(
            status_code=400,
            detail=f"Service '{service}' is not in the allowed list",
        )


def _run(cmd: list[str], timeout: int = 30) -> dict:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Command timed out")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Unauthenticated liveness probe."""
    return {"status": "ok"}


@app.get("/status/{service}")
def status(service: str, api_key: str = Security(_api_key_header)):
    """Return whether the systemd unit is active."""
    _auth(api_key)
    _validate_service(service)
    result = _run(_STATUS_CMDS[service])
    return {
        "service": service,
        "active": result["returncode"] == 0,
        "state": result["stdout"] or result["stderr"],
    }


@app.get("/logs/{service}")
def logs(
    service: str,
    n: int = Query(default=50, ge=1, le=1000),
    api_key: str = Security(_api_key_header),
):
    """Return the last N lines of journald logs for the service (1–1000)."""
    _auth(api_key)
    _validate_service(service)
    # n is a FastAPI-validated int in [1, 1000]; shell=False; journalctl accepts
    # integer -n args safely.  CodeQL py/command-line-injection is a false positive here.
    base_cmd = _LOGS_CMDS[service]
    result = _run(base_cmd + ["-n", str(n)], timeout=15)  # noqa: S603
    return {
        "service": service,
        "lines": result["stdout"].splitlines(),
        "returncode": result["returncode"],
    }


@app.post("/restart/{service}")
def restart(service: str, api_key: str = Security(_api_key_header)):
    """Restart the systemd unit."""
    _auth(api_key)
    _validate_service(service)
    result = _run(_RESTART_CMDS[service], timeout=60)
    if result["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=result["stderr"] or "Restart failed",
        )
    return {"service": service, "action": "restarted", "ok": True}


@app.post("/deploy/{service}")
def deploy(service: str, api_key: str = Security(_api_key_header)):
    """Run git pull in the service repo directory, then restart the unit."""
    _auth(api_key)
    _validate_service(service)

    if service not in _DEPLOY_CMDS:
        raise HTTPException(
            status_code=400,
            detail=f"Deployment not configured for service '{service}'.",
        )

    pull = _run(_DEPLOY_CMDS[service], timeout=60)
    if pull["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git pull failed: {pull['stderr'] or pull['stdout']}",
        )

    restart_result = _run(_RESTART_CMDS[service], timeout=60)
    if restart_result["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git pull succeeded but restart failed: {restart_result['stderr']}",
        )

    return {
        "service": service,
        "action": "deployed",
        "pull_output": pull["stdout"],
        "ok": True,
    }

