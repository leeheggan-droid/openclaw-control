"""
VPS Control API
---------------
FastAPI service exposing status, logs, restart, deploy, discovery, and job
execution endpoints for the systemd-managed bot services running on srv1501082.

Auth:  Authorization: Bearer <VPS_CONTROL_API_KEY>
Port:  8765 (HTTP — TLS termination via nginx is a future step)

Run:
    uvicorn api:app --host 0.0.0.0 --port 8765
"""

import json
import os
import secrets
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_raw_key = os.environ.get("VPS_CONTROL_API_KEY", "")
if not _raw_key:
    raise RuntimeError("VPS_CONTROL_API_KEY environment variable is required but not set")

API_KEY: str = _raw_key

_CONTRACT_PATH = Path(__file__).with_name("control_contract.json")
CONTROL_CONTRACT: dict[str, Any] = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))

SERVICE_METADATA: dict[str, dict[str, Any]] = {
    service["id"]: service for service in CONTROL_CONTRACT["services"]
}
ACTION_METADATA: dict[str, dict[str, Any]] = {
    action["id"]: action for action in CONTROL_CONTRACT["actions"]
}
OPERATOR_METADATA: dict[str, dict[str, Any]] = {
    operator["id"]: operator for operator in CONTROL_CONTRACT["operators"]
}

ALLOWED_SERVICES: set[str] = set(SERVICE_METADATA)
DEPLOYABLE_SERVICES: set[str] = {
    service_id for service_id, metadata in SERVICE_METADATA.items() if metadata["deployable"]
}
DEPLOY_PATHS: dict[str, str] = {
    service_id: metadata["deploy_path"]
    for service_id, metadata in SERVICE_METADATA.items()
    if metadata["deployable"]
}

# ---------------------------------------------------------------------------
# Pre-built command tables — all subprocess args are Python literals.
# User input is ONLY used as a dict key; it never flows into these lists.
# ---------------------------------------------------------------------------

_STATUS_CMDS: dict[str, list[str]] = {
    service_id: ["systemctl", "is-active", service_id]
    for service_id in ALLOWED_SERVICES
}

_LOGS_CMDS: dict[str, list[str]] = {
    service_id: ["sudo", "journalctl", "-u", service_id, "--no-pager", "--output=short-iso"]
    for service_id in ALLOWED_SERVICES
}

_RESTART_CMDS: dict[str, list[str]] = {
    service_id: ["sudo", "systemctl", "restart", service_id]
    for service_id in ALLOWED_SERVICES
}

_DEPLOY_CMDS: dict[str, list[str]] = {
    service_id: ["git", "-C", path, "pull"] for service_id, path in DEPLOY_PATHS.items()
}

_DEFAULT_LOG_LINES = 50
_DIAGNOSTIC_LOG_LINES = 20
_MAX_LOG_LINES = 1000
_CONFIRMATION_ERROR = "confirmation_required"

# Stable states returned by `systemctl is-active`.
_STABLE_STATES: frozenset[str] = frozenset({"active", "inactive", "failed"})

JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="VPS Control API", version=CONTROL_CONTRACT["api_version"])

_api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class JobRequest(BaseModel):
    action: str
    service: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False
    confirmation_note: Optional[str] = None
    operator: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


def _validate_action(action: str) -> dict[str, Any]:
    if action not in ACTION_METADATA:
        raise HTTPException(
            status_code=400,
            detail=f"Action '{action}' is not in the control contract",
        )
    return ACTION_METADATA[action]


def _service_summary(service: str) -> str:
    return SERVICE_METADATA[service]["display_name"]


def _result_dict(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "returncode": result.returncode,
    }


def _normalize_state(result: dict[str, Any]) -> dict[str, object]:
    """Return a normalised service-state dict from a `systemctl is-active` result."""
    raw = result["stdout"].strip() or result["stderr"].strip()
    if raw in _STABLE_STATES:
        normalized = raw
    elif raw in ("activating", "reloading"):
        normalized = "active"
    elif raw == "deactivating":
        normalized = "inactive"
    else:
        normalized = "unknown"

    out: dict[str, object] = {
        "active": result["returncode"] == 0,
        "state": normalized,
    }
    if raw != normalized:
        out["raw_status"] = raw
    return out


def _run_status_command(service: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            _STATUS_CMDS[service],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return _result_dict(result)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Command timed out") from exc


def _run_logs_command(service: str) -> dict[str, Any]:
    command = _LOGS_CMDS[service] + ["-n", str(_MAX_LOG_LINES)]
    try:
        # `service` is validated against the allow-list and the subprocess call
        # uses a fixed argv shape. The caller's requested `n` is applied in
        # Python after the bounded journalctl read returns.
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return _result_dict(result)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Command timed out") from exc


def _run_restart_command(service: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            _RESTART_CMDS[service],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return _result_dict(result)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Command timed out") from exc


def _run_git_rev_parse(service: str) -> dict[str, Any]:
    command = ["git", "-C", DEPLOY_PATHS[service], "rev-parse", "HEAD"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return _result_dict(result)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Command timed out") from exc


def _run_git_fetch(service: str) -> dict[str, Any]:
    command = ["git", "-C", DEPLOY_PATHS[service], "fetch"]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return _result_dict(result)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Command timed out") from exc


def _run_git_pull(service: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            _DEPLOY_CMDS[service],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return _result_dict(result)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Command timed out") from exc


def _parse_log_lines(value: Any, *, default: int = _DEFAULT_LOG_LINES) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Parameter 'n' must be an integer") from exc
    if not 1 <= n <= _MAX_LOG_LINES:
        raise HTTPException(
            status_code=400,
            detail=f"Parameter 'n' must be between 1 and {_MAX_LOG_LINES}",
        )
    return n


def _build_operation_response(
    *,
    action: str,
    service: Optional[str],
    ok: bool,
    summary: str,
    data: dict[str, Any],
    reason: Optional[str] = None,
    artifacts: Optional[dict[str, Any]] = None,
    operator: Optional[str] = None,
    job: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": ok,
        "status": "succeeded" if ok else "failed",
        "action": action,
        "service": service,
        "summary": summary,
        "reason": reason,
        "operator": operator,
        "data": data,
        "artifacts": artifacts or {},
    }
    if job is not None:
        response["job"] = job
    return response


def _action_requires_confirmation(action: str, service: Optional[str]) -> bool:
    action_meta = ACTION_METADATA[action]
    if action_meta["requires_confirmation"]:
        return True
    if not service:
        return False
    service_meta = SERVICE_METADATA[service]
    # Money-risk services require confirmation for any non-read action even if
    # that action is not globally marked as confirmation-gated.
    return bool(service_meta.get("money_risk")) and action_meta["category"] != "read"


def _confirmation_failure_job(
    *,
    action: str,
    service: Optional[str],
    operator: str,
    parameters: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    job = {
        "id": str(uuid4()),
        "status": "failed",
        "action": action,
        "service": service,
        "operator": operator,
        "parameters": parameters,
        "submitted_at": _timestamp(),
        "started_at": _timestamp(),
        "completed_at": _timestamp(),
        "error_code": _CONFIRMATION_ERROR,
        "confirmed": False,
    }
    job["result"] = _build_operation_response(
        action=action,
        service=service,
        ok=False,
        summary=f"{action} blocked by control policy",
        reason=reason,
        operator=operator,
        data={"policy": "confirmation_required"},
        artifacts={},
        job={k: v for k, v in job.items() if k != "result"},
    )
    with _JOBS_LOCK:
        JOBS[job["id"]] = job
    return job


def _get_status_summary(service: str) -> dict[str, Any]:
    result = _run_status_command(service)
    return {"service": service, **_normalize_state(result)}


def _get_log_snapshot(service: str, n: int) -> dict[str, Any]:
    # n is validated in FastAPI or `_parse_log_lines`. We always read a bounded
    # max window from journald and slice in Python to avoid dynamic command args.
    result = _run_logs_command(service)
    all_lines = result["stdout"].splitlines()
    lines = all_lines[-n:] if result["returncode"] == 0 else []
    return {
        "service": service,
        "lines": lines,
        "returncode": result["returncode"],
        "stderr": result["stderr"],
    }


def _execute_status(service: str, *, operator: Optional[str] = None) -> dict[str, Any]:
    status_summary = _get_status_summary(service)
    return _build_operation_response(
        action="status",
        service=service,
        ok=True,
        summary=f"{_service_summary(service)} is {status_summary['state']}",
        operator=operator,
        data=status_summary,
        artifacts={"status_summary": status_summary},
    )


def _execute_logs(
    service: str,
    *,
    n: int,
    operator: Optional[str] = None,
) -> dict[str, Any]:
    logs_result = _get_log_snapshot(service, n)
    return _build_operation_response(
        action="logs",
        service=service,
        ok=logs_result["returncode"] == 0,
        summary=f"Fetched {len(logs_result['lines'])} log lines for {_service_summary(service)}",
        reason=logs_result["stderr"] or None,
        operator=operator,
        data={
            "service": service,
            "line_count": len(logs_result["lines"]),
            "returncode": logs_result["returncode"],
        },
        artifacts={"log_lines": logs_result["lines"]},
    )


def _execute_diagnostics(
    service: str,
    *,
    n: int,
    operator: Optional[str] = None,
) -> dict[str, Any]:
    status_summary = _get_status_summary(service)
    logs_result = _get_log_snapshot(service, n)
    ok = logs_result["returncode"] == 0
    reason = None if ok else (logs_result["stderr"] or "Log collection failed")
    return _build_operation_response(
        action="diagnostics",
        service=service,
        ok=ok,
        summary=(
            f"{_service_summary(service)} diagnostics collected"
            if ok
            else f"{_service_summary(service)} diagnostics collected with log errors"
        ),
        reason=reason,
        operator=operator,
        data={
            "service": service,
            "status_summary": status_summary,
            "log_line_count": len(logs_result["lines"]),
            "log_returncode": logs_result["returncode"],
        },
        artifacts={
            "status_summary": status_summary,
            "log_lines": logs_result["lines"],
        },
    )


def _execute_restart(service: str, *, operator: Optional[str] = None) -> dict[str, Any]:
    result = _run_restart_command(service)
    if result["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=result["stderr"] or "Restart failed",
        )

    diagnostics = _execute_diagnostics(
        service,
        n=_DIAGNOSTIC_LOG_LINES,
        operator=operator,
    )
    diagnostics["action"] = "restart"
    diagnostics["summary"] = f"Restarted {_service_summary(service)}"
    diagnostics["data"]["restart"] = {"ok": True}
    diagnostics["artifacts"]["restart_result"] = {
        "success": True,
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }
    return diagnostics


def _execute_deploy(service: str, *, operator: Optional[str] = None) -> dict[str, Any]:
    if service not in _DEPLOY_CMDS:
        raise HTTPException(
            status_code=400,
            detail=f"Deployment not configured for service '{service}'.",
        )

    repo_path = DEPLOY_PATHS[service]

    commit_before_result = _run_git_rev_parse(service)
    commit_before = (
        commit_before_result["stdout"] if commit_before_result["returncode"] == 0 else "unknown"
    )

    fetch = _run_git_fetch(service)
    if fetch["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git fetch failed: {fetch['stderr'] or fetch['stdout']}",
        )

    pull = _run_git_pull(service)
    if pull["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git pull failed: {pull['stderr'] or pull['stdout']}",
        )

    commit_after_result = _run_git_rev_parse(service)
    commit_after = (
        commit_after_result["stdout"] if commit_after_result["returncode"] == 0 else "unknown"
    )

    restart_result = _run_restart_command(service)
    if restart_result["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git pull succeeded but restart failed: {restart_result['stderr']}",
        )

    diagnostics = _execute_diagnostics(
        service,
        n=_DIAGNOSTIC_LOG_LINES,
        operator=operator,
    )
    diagnostics["action"] = "deploy"
    diagnostics["summary"] = f"Deployed {_service_summary(service)}"
    diagnostics["data"].update(
        {
            "repo_path": repo_path,
            "commit_before": commit_before,
            "commit_after": commit_after,
        }
    )
    diagnostics["artifacts"].update(
        {
            "fetch_output": fetch["stdout"],
            "pull_output": pull["stdout"],
            "restart_result": {
                "success": True,
                "stdout": restart_result["stdout"],
                "stderr": restart_result["stderr"],
            },
        }
    )
    return diagnostics


def _execute_action(
    action: str,
    *,
    service: str,
    parameters: dict[str, Any],
    operator: Optional[str] = None,
) -> dict[str, Any]:
    if action == "status":
        return _execute_status(service, operator=operator)
    if action == "logs":
        return _execute_logs(
            service,
            n=_parse_log_lines(parameters.get("n")),
            operator=operator,
        )
    if action == "diagnostics":
        return _execute_diagnostics(
            service,
            n=_parse_log_lines(parameters.get("n"), default=_DIAGNOSTIC_LOG_LINES),
            operator=operator,
        )
    if action == "restart":
        return _execute_restart(service, operator=operator)
    if action == "deploy":
        return _execute_deploy(service, operator=operator)
    raise HTTPException(status_code=400, detail=f"Unsupported action '{action}'")


def _build_job(
    request: JobRequest,
    *,
    operator: str,
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "status": "queued",
        "action": request.action,
        "service": request.service,
        "operator": operator,
        "parameters": request.parameters,
        "submitted_at": _timestamp(),
        "started_at": None,
        "completed_at": None,
        "confirmed": request.confirmed,
        "confirmation_note": request.confirmation_note,
    }


def _store_job(job: dict[str, Any]) -> None:
    with _JOBS_LOCK:
        JOBS[job["id"]] = job


def _load_job(job_id: str) -> Optional[dict[str, Any]]:
    with _JOBS_LOCK:
        return JOBS.get(job_id)


def _contract_snapshot() -> dict[str, Any]:
    return {
        **CONTROL_CONTRACT,
        "links": {
            "contract": "/contract",
            "capabilities": "/capabilities",
            "actions": "/actions",
            "services": "/services",
            "operators": "/operators",
            "jobs": "/jobs",
        },
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    """Unauthenticated liveness probe."""
    return {"status": "ok"}


@app.get("/contract")
def contract(api_key: str = Security(_api_key_header)) -> dict[str, Any]:
    """Machine-readable control contract for Link and other LLM managers."""
    _auth(api_key)
    return _contract_snapshot()


@app.get("/capabilities")
def capabilities(api_key: str = Security(_api_key_header)) -> dict[str, Any]:
    """High-level discovery endpoint summarising services, actions, and policies."""
    _auth(api_key)
    return {
        "contract_version": CONTROL_CONTRACT["contract_version"],
        "api_version": CONTROL_CONTRACT["api_version"],
        "manager": CONTROL_CONTRACT["manager"],
        "operators": CONTROL_CONTRACT["operators"],
        "services": CONTROL_CONTRACT["services"],
        "actions": CONTROL_CONTRACT["actions"],
        "policies": CONTROL_CONTRACT["policies"],
    }


@app.get("/services")
def services(api_key: str = Security(_api_key_header)) -> dict[str, Any]:
    _auth(api_key)
    return {
        "contract_version": CONTROL_CONTRACT["contract_version"],
        "services": CONTROL_CONTRACT["services"],
    }


@app.get("/actions")
def actions(api_key: str = Security(_api_key_header)) -> dict[str, Any]:
    _auth(api_key)
    return {
        "contract_version": CONTROL_CONTRACT["contract_version"],
        "actions": CONTROL_CONTRACT["actions"],
    }


@app.get("/operators")
def operators(api_key: str = Security(_api_key_header)) -> dict[str, Any]:
    _auth(api_key)
    return {
        "contract_version": CONTROL_CONTRACT["contract_version"],
        "manager": CONTROL_CONTRACT["manager"],
        "operators": CONTROL_CONTRACT["operators"],
    }


@app.get("/status/{service}")
def status(service: str, api_key: str = Security(_api_key_header)) -> dict[str, Any]:
    """Return whether the systemd unit is active."""
    _auth(api_key)
    _validate_service(service)
    result = _run_status_command(service)
    return {"service": service, **_normalize_state(result)}


@app.get("/logs/{service}")
def logs(
    service: str,
    n: int = Query(default=_DEFAULT_LOG_LINES, ge=1, le=_MAX_LOG_LINES),
    api_key: str = Security(_api_key_header),
) -> dict[str, Any]:
    """Return the last N lines of journald logs for the service."""
    _auth(api_key)
    _validate_service(service)
    result = _get_log_snapshot(service, n)
    return {
        "service": service,
        "lines": result["lines"],
        "returncode": result["returncode"],
    }


@app.get("/diagnostics/{service}")
def diagnostics(
    service: str,
    n: int = Query(default=_DIAGNOSTIC_LOG_LINES, ge=1, le=_MAX_LOG_LINES),
    api_key: str = Security(_api_key_header),
) -> dict[str, Any]:
    """Return a standardized read-only diagnostics bundle for a service."""
    _auth(api_key)
    _validate_service(service)
    return _execute_diagnostics(service, n=n, operator="read-only-operator")


@app.post("/restart/{service}")
def restart(service: str, api_key: str = Security(_api_key_header)) -> dict[str, Any]:
    """Legacy endpoint: restart the systemd unit."""
    _auth(api_key)
    _validate_service(service)
    result = _run_restart_command(service)
    if result["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=result["stderr"] or "Restart failed",
        )
    return {"service": service, "action": "restarted", "ok": True}


@app.post("/deploy/{service}")
def deploy(service: str, api_key: str = Security(_api_key_header)) -> dict[str, Any]:
    """
    Legacy endpoint: run git fetch + git pull in the service repo directory, then
    restart the unit. Returns detailed deployment information for compatibility.
    """
    _auth(api_key)
    _validate_service(service)

    if service not in _DEPLOY_CMDS:
        raise HTTPException(
            status_code=400,
            detail=f"Deployment not configured for service '{service}'.",
        )

    repo_path = DEPLOY_PATHS[service]

    commit_before_result = _run_git_rev_parse(service)
    commit_before = (
        commit_before_result["stdout"] if commit_before_result["returncode"] == 0 else "unknown"
    )

    fetch = _run_git_fetch(service)
    if fetch["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git fetch failed: {fetch['stderr'] or fetch['stdout']}",
        )

    pull = _run_git_pull(service)
    if pull["returncode"] != 0:
        raise HTTPException(
            status_code=500,
            detail=f"git pull failed: {pull['stderr'] or pull['stdout']}",
        )

    commit_after_result = _run_git_rev_parse(service)
    commit_after = (
        commit_after_result["stdout"] if commit_after_result["returncode"] == 0 else "unknown"
    )

    restart_result = _run_restart_command(service)
    restart_success = restart_result["returncode"] == 0
    if not restart_success:
        raise HTTPException(
            status_code=500,
            detail=f"git pull succeeded but restart failed: {restart_result['stderr']}",
        )

    status_summary = _get_status_summary(service)
    logs_result = _get_log_snapshot(service, _DIAGNOSTIC_LOG_LINES)
    log_tail = logs_result["lines"] if logs_result["returncode"] == 0 else []
    if logs_result["returncode"] == 0:
        log_error = None
    else:
        log_error = logs_result["stderr"] or "log collection failed"

    return {
        "service": service,
        "action": "deployed",
        "success": True,
        "repo_path": repo_path,
        "commit_before": commit_before,
        "commit_after": commit_after,
        "fetch_output": fetch["stdout"],
        "pull_output": pull["stdout"],
        "restart_result": {
            "success": restart_success,
            "stdout": restart_result["stdout"],
            "stderr": restart_result["stderr"],
        },
        "status_summary": status_summary,
        "log_tail": log_tail,
        "log_error": log_error,
        "ok": True,
    }


@app.post("/jobs")
def create_job(
    request: JobRequest,
    api_key: str = Security(_api_key_header),
) -> dict[str, Any]:
    """Submit a bounded control action and receive a structured job result."""
    _auth(api_key)

    action_meta = _validate_action(request.action)
    operator = request.operator or action_meta["operator"]
    if operator not in OPERATOR_METADATA:
        raise HTTPException(
            status_code=400,
            detail=f"Operator '{operator}' is not in the control contract",
        )

    if action_meta["requires_service"]:
        if not request.service:
            raise HTTPException(
                status_code=400,
                detail=f"Action '{request.action}' requires a service",
            )
        _validate_service(request.service)

    if _action_requires_confirmation(request.action, request.service):
        service_name = request.service or "selected target"
        requires_note = bool(
            request.service and SERVICE_METADATA[request.service].get("money_risk")
        )
        missing_confirmation = not request.confirmed
        missing_note = requires_note and not request.confirmation_note
        if missing_confirmation or missing_note:
            reason = (
                f"{request.action} on {service_name} requires explicit confirmation in control"
                if not requires_note
                else (
                    f"{request.action} on {service_name} requires explicit confirmation and "
                    "a confirmation_note recording the manual safety check"
                )
            )
            job = _confirmation_failure_job(
                action=request.action,
                service=request.service,
                operator=operator,
                parameters=request.parameters,
                reason=reason,
            )
            return job

    job = _build_job(request, operator=operator)
    _store_job(job)

    try:
        job["status"] = "running"
        job["started_at"] = _timestamp()
        result = _execute_action(
            request.action,
            service=request.service or "",
            parameters=request.parameters,
            operator=operator,
        )
        job["status"] = "succeeded" if result["ok"] else "failed"
        job["completed_at"] = _timestamp()
        job["result"] = {
            **result,
            "job": {k: v for k, v in job.items() if k != "result"},
        }
    except HTTPException as exc:
        job["status"] = "failed"
        job["completed_at"] = _timestamp()
        job["error_code"] = "execution_failed"
        job["result"] = _build_operation_response(
            action=request.action,
            service=request.service,
            ok=False,
            summary=f"{request.action} failed for {request.service}",
            reason=str(exc.detail),
            operator=operator,
            data={"http_status": exc.status_code},
            artifacts={},
            job={k: v for k, v in job.items() if k != "result"},
        )

    return job


@app.get("/jobs/{job_id}")
def get_job(job_id: str, api_key: str = Security(_api_key_header)) -> dict[str, Any]:
    """Fetch a previously submitted job from the in-memory control room ledger."""
    _auth(api_key)
    job = _load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job
