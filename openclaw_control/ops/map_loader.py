"""openclaw_control/ops/map_loader.py

Loads, validates, and caches the OpenClaw ops map YAML as "core memory".
The map is reloaded automatically when its file modification time changes
(useful during development without a server restart).

Public API
----------
get_map()     -> dict          Parsed ops map; reloads if file has changed.
get_summary() -> str           Compressed text summary (≤ 3 000 chars) for prompt injection.
get_top_keys() -> list[str]    Top-level key names for the /ops/map health endpoint.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# File location
# ---------------------------------------------------------------------------

_MAP_FILE = Path(__file__).parent / "maps" / "openclaw_control_ops_map.yaml"

REQUIRED_KEYS: frozenset[str] = frozenset({
    "system_overview",
    "repository_structure",
    "container_map",
    "data_location_map",
    "vibe_capability_contract",
    "agent_behavior_rules",
    "known_limitations",
})

# Maximum characters for the compressed summary injected into agent prompts.
_MAX_SUMMARY_LENGTH: int = 3000

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_cache: dict[str, Any] = {}
_cache_mtime: float = 0.0


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _load_raw() -> dict[str, Any]:
    """Read, parse, and validate the YAML map file."""
    with open(_MAP_FILE, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError("Ops map YAML must be a top-level mapping.")
    missing = REQUIRED_KEYS - data.keys()
    if missing:
        raise ValueError(f"Ops map is missing required top-level keys: {sorted(missing)}")
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_map() -> dict[str, Any]:
    """Return the parsed ops map dict, reloading from disk if the file has changed."""
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(_MAP_FILE)
    except OSError:
        if _cache:
            return _cache
        raise
    if _cache and mtime == _cache_mtime:
        return _cache
    _cache = _load_raw()
    _cache_mtime = mtime
    return _cache


def get_top_keys() -> list[str]:
    """Return the sorted list of top-level keys in the ops map."""
    return sorted(get_map().keys())


def get_summary() -> str:
    """Return a compressed text summary (≤ 3 000 chars) suitable for prompt injection.

    The summary is generated from the live ops map so it stays in sync with the YAML.
    """
    try:
        data = get_map()
    except Exception:
        return "[OPS MAP UNAVAILABLE — check server logs]"

    lines: list[str] = ["=== OPS MAP CORE MEMORY ==="]

    # --- System overview -------------------------------------------------------
    overview = data.get("system_overview", {})
    desc = (overview.get("description") or "").strip()
    if desc:
        lines.append(f"Bot: {desc[:200]}")
    vps = overview.get("vps_defaults", {})
    repo_default = vps.get("repo_dir_default", "/opt/openclaw-crypto")
    lines.append(f"VPS repo default: {repo_default}")

    # --- Primary container log commands ----------------------------------------
    cmap = data.get("container_map", {})
    primary = cmap.get("vps_containers", {}).get("primary", {})
    container_name = primary.get("name", "openclaw-orchestrator")
    log_cmds = primary.get("log_commands", {})
    lines.append(f"\nPRIMARY CONTAINER: {container_name}")
    if log_cmds.get("tail_500"):
        lines.append(f"  logs: {log_cmds['tail_500']}")
    if log_cmds.get("keyword_grep"):
        lines.append(f"  grep: {log_cmds['keyword_grep']}")
    discovery = cmap.get("vps_containers", {}).get("discovery", "")
    if discovery:
        lines.append(f"  ps:   {discovery}")

    # --- Data retrieval commands (report_id → first command) -------------------
    dlm = data.get("data_location_map", {})
    report_cmds = dlm.get("report_commands", {})
    if report_cmds:
        lines.append("\nDATA RETRIEVAL (use these commands; do not invent paths):")
        for rid, block in report_cmds.items():
            cmds = block.get("commands", [])
            absence = block.get("absence_means", "")
            if cmds:
                lines.append(f"  {rid}:")
                for cmd in cmds:
                    lines.append(f"    $ {cmd}")
                if absence:
                    lines.append(f"    absence: {absence}")
    priority = dlm.get("inspection_priority", [])
    if priority:
        lines.append("\nINSPECTION ORDER:")
        for step in priority[:5]:
            lines.append(f"  {step}")

    # --- Vibe capability contract ----------------------------------------------
    vcc = data.get("vibe_capability_contract", {})
    may = vcc.get("vibe_may_do_autonomously", [])
    must_not = vcc.get("vibe_must_never_do_without_operator_approval", [])
    if may:
        lines.append("\nVIBE MAY DO (autonomously):")
        for item in may[:5]:
            lines.append(f"  ✓ {item}")
    if must_not:
        lines.append("VIBE MUST NOT DO without approval:")
        for item in must_not[:5]:
            lines.append(f"  ✗ {item}")

    # --- Agent behavior rules --------------------------------------------------
    abr = data.get("agent_behavior_rules", {})
    global_rules = abr.get("global_rules", [])
    if global_rules:
        lines.append("\nAGENT RULES (all agents):")
        for rule in global_rules[:5]:
            lines.append(f"  • {rule}")

    # --- Known limitations -----------------------------------------------------
    limits = data.get("known_limitations", {})
    if limits:
        lines.append("\nKNOWN LIMITATIONS:")
        for key, block in limits.items():
            summary = block.get("summary", "") if isinstance(block, dict) else str(block)
            lines.append(f"  [{key}] {summary}")

    result = "\n".join(lines)
    if len(result) > _MAX_SUMMARY_LENGTH:
        result = result[:_MAX_SUMMARY_LENGTH - 3] + "..."
    return result
