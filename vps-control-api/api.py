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
import subprocess
from typing import Optional

from fastapi import FastAPI, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_raw_key = os.environ.get("VPS_CONTROL_API_KEY", "")
if not _raw_key:
    raise RuntimeError("VPS_CONTROL_API_KEY environment variable must be set")

API_KEY: str = _raw_key

# Exact systemd unit names that this API is permitted to manage.
ALLOWED_SERVICES: set[str] = {
    "openclaw-agent.service",
    "openclaw-crypto.service",
    "openclaw-vibe-gateway.service",
    "alpaca_orb_bite_bot.service",
    "linkedin-news.timer",
    "linkedin-news.service",
}

# Map unit name → git repo root for the /deploy endpoint.
# Set the path for any unit that supports git-pull-based deployment.
# Run `systemctl cat <unit>` on the VPS to confirm working directories.
DEPLOY_MAP: dict[str, str] = {
    "openclaw-agent.service": "/opt/openclaw-agent",
    "openclaw-crypto.service": "/home/jacks/openclaw-crypto",
    # alpaca_orb_bite_bot — confirm path via `systemctl cat alpaca_orb_bite_bot.service`
    # "alpaca_orb_bite_bot.service": "/home/jacks/alpaca_orb_bite_bot",
}

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
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _get_service(service: str) -> str:
    """Validate service against the allowlist and return the canonical name from
    our own controlled set — never passing raw user input to subprocess."""
    if service not in ALLOWED_SERVICES:
        raise HTTPException(
            status_code=400,
            detail=f"Service '{service}' is not in the allowed list",
        )
    # Return from our constant set so the value never originates from user input.
    return next(s for s in ALLOWED_SERVICES if s == service)


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
    svc = _get_service(service)
    result = _run(["systemctl", "is-active", svc])
    return {
        "service": svc,
        "active": result["returncode"] == 0,
        "state": result["stdout"] or result["stderr"],
    }


@app.get("/logs/{service}")
def logs(
    service: str,
    n: int = 50,
    api_key: str = Security(_api_key_header),
):
    """Return the last N lines of journald logs for the service."""
    _auth(api_key)
    svc = _get_service(service)
    result = _run(
        ["sudo", "journalctl", "-u", svc, f"-n{n}", "--no-pager", "--output=short-iso"],
        timeout=15,
    )
    return {
        "service": svc,
        "lines": result["stdout"].splitlines(),
        "returncode": result["returncode"],
    }


@app.post("/restart/{service}")
def restart(service: str, api_key: str = Security(_api_key_header)):
    """Restart the systemd unit."""
    _auth(api_key)
    svc = _get_service(service)
    result = _run(["sudo", "systemctl", "restart", svc], timeout=60)
    if result["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=result["stderr"] or "Restart failed",
        )
    return {"service": svc, "action": "restarted", "ok": True}


@app.post("/deploy/{service}")
def deploy(service: str, api_key: str = Security(_api_key_header)):
    """Run git pull in the service repo directory, then restart the unit."""
    _auth(api_key)
    svc = _get_service(service)

    repo_path = DEPLOY_MAP.get(svc)
    if not repo_path:
        raise HTTPException(
            status_code=400,
            detail=f"No deploy path configured for '{svc}'. Add it to DEPLOY_MAP in api.py.",
        )

    pull = _run(["git", "-C", repo_path, "pull"], timeout=60)
    if pull["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git pull failed: {pull['stderr'] or pull['stdout']}",
        )

    restart_result = _run(["sudo", "systemctl", "restart", svc], timeout=60)
    if restart_result["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git pull succeeded but restart failed: {restart_result['stderr']}",
        )

    return {
        "service": svc,
        "action": "deployed",
        "pull_output": pull["stdout"],
        "ok": True,
    }
