"""openclaw_control/vibe_reports.py

Extended Vibe report library for evidence-based agent memory.

Each report_id maps to a deterministic sequence of read-only SSH commands
ordered by reliability (fastest / most informative first).

Usage
-----
    from openclaw_control.vibe_reports import VIBE_REPORT_IDS, get_report_commands
    from openclaw_control.vibe_reports import extract_fingerprint_fields

Public API
----------
  VIBE_REPORT_IDS          frozenset[str]   All valid report IDs.
  get_report_commands(id)  list[str]        SSH commands for a given report ID.
  extract_fingerprint_fields(evidence)
                           tuple[str, str]  (container_start_times, git_head)
                                            extracted from an evidence string for
                                            fingerprint computation.
"""

from __future__ import annotations

import re
from openclaw_control.config import settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repo() -> str:
    return settings.repo_dir or "/opt/openclaw-crypto"


# ---------------------------------------------------------------------------
# Report definitions
# ---------------------------------------------------------------------------
# Commands are ordered: fastest/most reliable first, slower/less likely last.
# All commands MUST be read-only (no writes, no mutations, no side effects).

def _build_reports() -> dict[str, list[str]]:
    repo = _repo()
    return {
        # ── Container/infrastructure health ─────────────────────────────────
        "container_health": [
            "docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.State}}' 2>/dev/null || echo '[docker unavailable]'",
            (
                "docker ps -q 2>/dev/null | xargs -r docker inspect "
                "--format '{{.Name}} status={{.State.Status}} started={{.State.StartedAt}} "
                "restarts={{.RestartCount}}' 2>/dev/null || echo '[no containers]'"
            ),
        ],

        # ── Last trade executed ──────────────────────────────────────────────
        "last_trade": [
            # alpaca_orb_bite_bot is the primary trading bot container; fall back
            # to openclaw-orchestrator which may also log trade events.
            "docker logs --tail=2000 alpaca_orb_bite_bot 2>&1 | grep -iE 'fill|filled|executed|order|trade' | tail -20",
            "docker logs --tail=2000 openclaw-orchestrator 2>&1 | grep -iE 'fill|filled|executed|order|trade' | tail -20",
            f"find {repo} -name '*.log' 2>/dev/null | xargs grep -l 'trade\\|fill\\|executed' 2>/dev/null | head -3",
            f"find {repo} -name '*.db' -o -name '*.sqlite' 2>/dev/null | head -3",
        ],

        # ── Trade history (last 7 days window in logs) ───────────────────────
        "trade_history_7d": [
            # Same dual-container fallback as last_trade; 5 000-line window to
            # capture up to ~7 days of activity depending on log verbosity.
            "docker logs --tail=5000 alpaca_orb_bite_bot 2>&1 | grep -iE 'fill|filled|executed|order|trade' | tail -100",
            "docker logs --tail=5000 openclaw-orchestrator 2>&1 | grep -iE 'fill|filled|executed|order|trade' | tail -100",
            f"find {repo} -name '*.db' -o -name '*.csv' 2>/dev/null | head -5",
            f"find {repo} -name 'trades*' -o -name 'history*' -o -name 'orders*' 2>/dev/null | head -5",
        ],

        # ── P&L snapshot ─────────────────────────────────────────────────────
        "pnl_snapshot": [
            # Same dual-container fallback; P&L keywords cover most log formats.
            "docker logs --tail=500 alpaca_orb_bite_bot 2>&1 | grep -iE 'pnl|sharpe|drawdown|equity|return|profit|loss' | tail -30",
            "docker logs --tail=500 openclaw-orchestrator 2>&1 | grep -iE 'pnl|sharpe|drawdown|equity|return|profit|loss' | tail -30",
            f"find {repo} -name 'pnl*' -o -name 'performance*' -o -name 'equity*' 2>/dev/null | head -5",
            f"find {repo} -name '*.json' 2>/dev/null | xargs grep -l 'pnl\\|sharpe\\|equity' 2>/dev/null | head -3",
        ],

        # ── HALT / safety state ──────────────────────────────────────────────
        "halt_status": [
            # Same dual-container fallback; HALT keywords cover safety / risk checks.
            "docker logs --tail=200 alpaca_orb_bite_bot 2>&1 | grep -iE 'HALT|HALTED|risk|bypass|multiplier|paused|stopped' | tail -20",
            "docker logs --tail=200 openclaw-orchestrator 2>&1 | grep -iE 'HALT|HALTED|risk|bypass|multiplier|paused|stopped' | tail -20",
            (
                "docker inspect openclaw-orchestrator "
                "--format '{{.Name}} state={{.State.Status}}' 2>/dev/null || echo '[container not found]'"
            ),
            f"grep -RIn 'HALT\\|halt_state\\|is_halted' {repo} 2>/dev/null | head -10",
        ],

        # ── Git HEAD on VPS (used for fingerprinting) ─────────────────────────
        "git_head": [
            f"cd {repo} && git rev-parse HEAD 2>/dev/null || echo '[unavailable]'",
            f"cd {repo} && git log -1 --oneline 2>/dev/null || echo '[unavailable]'",
        ],
    }


# Build once at import time; rebuilt lazily if repo_dir could change.
# In practice repo_dir is read once from env so this is safe.
_REPORTS: dict[str, list[str]] = _build_reports()

VIBE_REPORT_IDS: frozenset[str] = frozenset(_REPORTS.keys())


def get_report_commands(report_id: str) -> list[str]:
    """Return the ordered list of SSH commands for *report_id*.

    Returns an empty list for unknown IDs (callers should validate first).
    """
    return list(_REPORTS.get(report_id.lower(), []))


# ---------------------------------------------------------------------------
# Fingerprint field extraction
# ---------------------------------------------------------------------------

# Patterns for extracting fields from evidence strings produced by run_vibe_report /
# _autopilot_gather_evidence.
_STARTED_AT_RE = re.compile(r"started=(\S+)")
_GIT_HEAD_RE = re.compile(r"\b([0-9a-f]{40})\b")
_GIT_SHORT_RE = re.compile(r"^([0-9a-f]{6,10})\s", re.MULTILINE)


def extract_fingerprint_fields(evidence: str) -> tuple[str, str]:
    """Extract (container_start_times, git_head) from an evidence string.

    Both values may be empty strings if the evidence does not contain the
    relevant data.  The caller passes these to ``compute_fingerprint`` in
    ``agent_memory``.
    """
    # Container start times: collect all 'started=<timestamp>' occurrences
    starts = _STARTED_AT_RE.findall(evidence)
    container_start_times = ",".join(sorted(set(starts)))

    # Git HEAD: prefer full 40-char SHA, fall back to short SHA
    full = _GIT_HEAD_RE.search(evidence)
    if full:
        git_head = full.group(1)
    else:
        short = _GIT_SHORT_RE.search(evidence)
        git_head = short.group(1) if short else ""

    return container_start_times, git_head
