"""Vibe Executor client for the openclaw-agent VPS service.

All VPS-side mutative operations MUST go through this module.
Only operations explicitly listed in VIBE_ALLOWLIST are permitted.
Every call is audit-logged via the standard logging framework (captured by
journald when the agent runs under systemd).

Supported transports (tried in order):
  1. HTTP    — when VIBE_API_BASE is set (e.g. https://vps.example.com:7000).
  2. SSH-CLI — when OPENCLAW_SSH_HOST is set; the Vibe CLI is invoked on the
               remote VPS via SSH so the control plane never assumes local
               access to VPS filesystems or Docker.
  3. CLI     — when /usr/local/bin/vibe (or VIBE_CLI_PATH) is present locally.
               This is a local-only fallback kept for development environments
               where the control plane and the VPS are the same machine.
  If none is available the call is rejected with exit_code=1.

Audit log format (one JSON line per call, level INFO):
  {
    "ts":          "<ISO-8601 UTC>",
    "action":      "<action_type>",
    "target":      "<target>",
    "params":      {<extra key/value pairs>},
    "exit_code":   <int>,
    "stdout_len":  <int>,
    "stderr_len":  <int>,
    "denied":      <bool>   // only present when denied
  }
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from datetime import datetime, timezone
from typing import Any

_LOG = logging.getLogger("vibe_client")

# ---------------------------------------------------------------------------
# Allowlist helpers
# ---------------------------------------------------------------------------


def _parse_allowlist(raw: str) -> set[tuple[str, str]]:
    """Parse VIBE_ALLOWLIST="action:target,action:target,..." into a set of tuples.

    A bare "action" token (no colon) is treated as ("action", "*"), matching
    that action against any target.
    """
    entries: set[tuple[str, str]] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        action, _, target = token.partition(":")
        entries.add((action.strip(), (target.strip() if target else "*")))
    return entries


def _is_allowed(action_type: str, target: str, allowlist: set[tuple[str, str]]) -> bool:
    """Return True when (action_type, target) or (action_type, '*') is in the allowlist."""
    return (action_type, target) in allowlist or (action_type, "*") in allowlist


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


def _audit(
    action_type: str,
    target: str,
    params: dict,
    result: dict,
) -> None:
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action_type,
        "target": target,
        "params": params,
        "exit_code": result.get("exit_code"),
        "stdout_len": len(result.get("stdout", "")),
        "stderr_len": len(result.get("stderr", "")),
    }
    if result.get("denied"):
        record["denied"] = True
    _LOG.info("VIBE_AUDIT %s", json.dumps(record))


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


def _call_http(
    api_base: str,
    api_key: str,
    action_type: str,
    target: str,
    extra_params: dict,
    timeout: int = 30,
) -> dict:
    """POST a Vibe action to the local HTTP gateway."""
    import requests  # already in requirements.txt

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Vibe-Key"] = api_key
    payload = {"action": action_type, "target": target, **extra_params}
    try:
        resp = requests.post(
            f"{api_base.rstrip('/')}/run",
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        data.setdefault("exit_code", 0)
        return data
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": str(exc)}


# ---------------------------------------------------------------------------
# CLI transport
# ---------------------------------------------------------------------------


def _call_cli(
    cli_path: str,
    action_type: str,
    target: str,
    extra_params: dict,
    timeout: int = 30,
) -> dict:
    """Invoke the local Vibe CLI with a JSON payload."""
    payload = json.dumps({"action": action_type, "target": target, **extra_params})
    try:
        proc = subprocess.run(
            [cli_path, "--json", payload],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        try:
            result = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            result = {"stdout": proc.stdout, "stderr": proc.stderr}
        result.setdefault("exit_code", proc.returncode)
        return result
    except subprocess.TimeoutExpired:
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Vibe CLI timed out after {timeout}s",
        }
    except FileNotFoundError:
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Vibe CLI not found at {cli_path}",
        }
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": str(exc)}


# ---------------------------------------------------------------------------
# SSH-CLI transport
# ---------------------------------------------------------------------------


def _call_ssh_cli(
    ssh_host: str,
    ssh_key: str,
    cli_path: str,
    action_type: str,
    target: str,
    extra_params: dict,
    timeout: int = 30,
) -> dict:
    """Invoke the Vibe CLI on the remote VPS via SSH.

    This is the correct transport when the control plane is not running on the
    VPS itself.  The JSON payload is passed as a single quoted argument to the
    remote CLI so no shell expansion occurs on the control-plane side.
    """
    payload = json.dumps({"action": action_type, "target": target, **extra_params})
    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=5",
    ]
    if ssh_key:
        ssh_cmd += ["-i", ssh_key]
    ssh_cmd += [ssh_host, f"{shlex.quote(cli_path)} --json {shlex.quote(payload)}"]
    try:
        proc = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        try:
            result = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            result = {"stdout": proc.stdout, "stderr": proc.stderr}
        result.setdefault("exit_code", proc.returncode)
        return result
    except subprocess.TimeoutExpired:
        return {
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Vibe SSH-CLI timed out after {timeout}s",
        }
    except Exception as exc:
        return {"exit_code": 1, "stdout": "", "stderr": str(exc)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def vibe_call(action_type: str, target: str, **extra_params: Any) -> dict:
    """Execute a single allowlisted Vibe operation.

    Deny-by-default: the call is rejected unless ``action_type:target`` (or
    ``action_type:*``) is present in the VIBE_ALLOWLIST environment variable.

    Parameters
    ----------
    action_type:
        The operation category, e.g. "restart_service", "tail_journal",
        "read_file".
    target:
        The specific service/file/path to operate on, e.g.
        "openclaw-agent.service" or "/var/log/syslog".
    **extra_params:
        Additional key/value pairs forwarded to the Vibe executor unchanged.

    Returns
    -------
    dict with at minimum:
        exit_code (int)   — 0 on success, non-zero on failure/denial.
        stdout    (str)   — captured standard output.
        stderr    (str)   — captured standard error or denial message.
        denied    (bool)  — True only when the call was blocked by the allowlist.
    """
    raw_allowlist = os.getenv("VIBE_ALLOWLIST", "")
    allowlist = _parse_allowlist(raw_allowlist)

    if not _is_allowed(action_type, target, allowlist):
        result: dict[str, Any] = {
            "exit_code": 1,
            "stdout": "",
            "stderr": (
                f"DENIED: action '{action_type}:{target}' is not in VIBE_ALLOWLIST. "
                "Add it explicitly to allow this operation."
            ),
            "denied": True,
        }
        _audit(action_type, target, extra_params, result)
        return result

    api_base = os.getenv("VIBE_API_BASE", "")
    api_key = os.getenv("VIBE_API_KEY", "")
    cli_path = os.getenv("VIBE_CLI_PATH", "/usr/local/bin/vibe")
    ssh_host = os.getenv("OPENCLAW_SSH_HOST", "")
    ssh_key = os.getenv("OPENCLAW_SSH_KEY", "")

    if api_base:
        result = _call_http(api_base, api_key, action_type, target, extra_params)
    elif ssh_host:
        # Control plane is remote: run the Vibe CLI on the VPS via SSH.
        # This is the correct transport when OPENCLAW_SSH_HOST is configured
        # and the control plane is not running on the VPS itself.
        result = _call_ssh_cli(ssh_host, ssh_key, cli_path, action_type, target, extra_params)
    elif os.path.isfile(cli_path):
        result = _call_cli(cli_path, action_type, target, extra_params)
    else:
        result = {
            "exit_code": 1,
            "stdout": "",
            "stderr": (
                "No Vibe transport available: set VIBE_API_BASE for HTTP transport, "
                "set OPENCLAW_SSH_HOST to invoke the Vibe CLI on the remote VPS via SSH, "
                "or install the Vibe CLI at /usr/local/bin/vibe (or override with "
                "VIBE_CLI_PATH) for local execution."
            ),
        }

    _audit(action_type, target, extra_params, result)
    return result
