import atexit as _atexit
import hashlib as _hashlib
import json as _json
import re as _re
import shlex as _shlex
import subprocess
import threading as _threading
import time as _time
import uuid as _uuid
from collections import Counter as _Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone as _tz
from typing import Any, NamedTuple

# ── Agents SDK (openai-agents) ────────────────────────────────────────────────
# Import core symbols. SQLiteSession / SessionSettings are not available in all
# builds; import them conditionally so the module still loads when the package
# is absent or incomplete.
try:
    from agents import Agent, ModelSettings, Runner, RunResult, SQLiteSession
    _AGENTS_SDK_AVAILABLE: bool = True
except ImportError:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "agents SDK not installed — agent features will be unavailable."
    )
    _AGENTS_SDK_AVAILABLE = False
    Agent = None  # type: ignore[assignment]
    ModelSettings = None  # type: ignore[assignment]
    RunResult = None  # type: ignore[assignment]

    class _SQLiteSessionStub:
        """No-op stub used when the agents SDK is not installed."""
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    SQLiteSession = _SQLiteSessionStub  # type: ignore[assignment,misc]

    class _RunnerStub:
        """Stub runner that raises a descriptive error when called."""
        @staticmethod
        def run_sync(*args: object, **kwargs: object) -> None:  # type: ignore[return]
            raise RuntimeError(
                "agents SDK is not installed; agent features are unavailable."
            )

    Runner = _RunnerStub  # type: ignore[assignment]

# SessionSettings was introduced in a later version of openai-agents; older
# installs don't export it.  Import conditionally so the module loads on any
# supported version — when unavailable we simply omit the session limit kwarg.
if _AGENTS_SDK_AVAILABLE:
    try:
        from agents import SessionSettings as _SessionSettingsCompat  # type: ignore[attr-defined]
        _SESSION_SETTINGS: Any = _SessionSettingsCompat(limit=100)
        _SESSION_KWARGS: dict[str, Any] = {"session_settings": _SESSION_SETTINGS}
    except ImportError:
        _SESSION_SETTINGS = None
        _SESSION_KWARGS = {}
else:
    _SESSION_SETTINGS = None
    _SESSION_KWARGS = {}

from openclaw_control import budget
from openclaw_control.budget import COO_BUDGET_MESSAGE
from openclaw_control.config import settings
from openclaw_control.github_tools import ALLOWED_REPOS as _GH_ALLOWED_REPOS, create_github_issue
# router.py has no agents-SDK dependency and is always safe to import.
from openclaw_control.agents.router import route_message
# The remaining agent modules all import from the agents SDK; guard them so that
# a missing / incomplete agents package does not prevent the server from starting.
try:
    from openclaw_control.agents.controller import controller
    from openclaw_control.agents.analysis_agent import analysis_agent
    from openclaw_control.agents.main_agent import main_agent
    from openclaw_control.agents.pnl_agent import pnl_agent
    from openclaw_control.agents.quant_agent import quant_agent
    from openclaw_control.agents.coo_agent import coo_agent
    from openclaw_control.agents.vibe_agent import vibe_planner, vibe_evaluator
    from openclaw_control.agents.investigate_agent import investigate_agent
except ImportError:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "One or more agent modules could not be imported — agent features will be unavailable."
    )
    controller = analysis_agent = main_agent = pnl_agent = None  # type: ignore[assignment]
    quant_agent = coo_agent = vibe_planner = vibe_evaluator = investigate_agent = None  # type: ignore[assignment]
from openclaw_control.ops import map_loader as _map_loader
from openclaw_control.memory import agent_memory as _memory
from openclaw_control import vibe_reports as _vibe_reports
from openclaw_control.evidence_pipeline import (
    run_with_evidence as _run_with_evidence,
    dispatch_coo_action as _dispatch_coo_action,
)

EXECUTOR = ThreadPoolExecutor(max_workers=2)

_ops_session = SQLiteSession("openclaw_ops_session", **_SESSION_KWARGS)
_analysis_session = SQLiteSession("openclaw_analysis_session", **_SESSION_KWARGS)
_main_session = SQLiteSession("openclaw_main_session", **_SESSION_KWARGS)
_pnl_session = SQLiteSession("openclaw_pnl_session", **_SESSION_KWARGS)
_quant_session = SQLiteSession("openclaw_quant_session", **_SESSION_KWARGS)
_coo_session = SQLiteSession("openclaw_coo_session", **_SESSION_KWARGS)
_vibe_session = SQLiteSession("openclaw_vibe_session", **_SESSION_KWARGS)

# ── Memory constants ──────────────────────────────────────────────────────────
# Number of leading fingerprint characters used for a quick config-mismatch check.
# The full fingerprint is 24 hex chars; 12 is enough to detect SSH host / repo_dir
# changes without requiring an SSH round-trip on every agent call.
_FINGERPRINT_PREFIX_LEN: int = 12

# Maximum characters stored per report in the agent memory snapshot.
# Derived summaries only — never raw full logs.
_MAX_REPORT_SUMMARY_LENGTH: int = 1000

# Maximum character length of a single string value injected from a memory snapshot
# into agent context.  Keeps prompts within token budgets while still conveying
# the key derived summary fields.
_MAX_CONTEXT_STRING_LENGTH: int = 500

# Set of agents for which Vibe probe evidence is persisted to the memory store.
# When adding a new tool-free agent that uses VIBE_REPORT_REQUEST, include it here.
_AGENTS_WITH_PROBE_PERSISTENCE: frozenset[str] = frozenset({"pnl", "quant"})

# ── Vibe Evidence Report mechanism ───────────────────────────────────────────
# Deterministic, read-only SSH command sequences per report_id.
# Commands are sourced from vibe_reports.py (primary) and the ops map YAML
# (secondary / legacy). Both are merged; vibe_reports.py takes precedence.

def _report_commands_from_map() -> dict[str, list[str]]:
    """Build report_id → [commands] from vibe_reports.py and the live ops map YAML.

    vibe_reports.py is the primary source; the YAML data_location_map is merged
    as a fallback so any operator customisations in the YAML are still honoured.
    Falls back to vibe_reports.py alone if the map cannot be loaded.
    """
    # Primary source: vibe_reports.py (always available)
    result: dict[str, list[str]] = {
        rid: _vibe_reports.get_report_commands(rid)
        for rid in _vibe_reports.VIBE_REPORT_IDS
    }
    # Secondary source: ops map YAML (adds any report_ids defined there but not in vibe_reports)
    try:
        data = _map_loader.get_map()
        report_section = data.get("data_location_map", {}).get("report_commands", {})
        for rid, block in (report_section or {}).items():
            if rid not in result:
                cmds = block.get("commands", [])
                if cmds:
                    result[rid] = list(cmds)
    except Exception:
        pass
    return result


# Regex to detect VIBE_REPORT_REQUEST in agent output.
_VIBE_REQUEST_RE = _re.compile(r"VIBE_REPORT_REQUEST:\s*(\w+)", _re.IGNORECASE)

# Patience settings for VIBE_REPORT_REQUEST retries.
# When a probe returns empty (e.g. a long-running backtest not yet complete),
# the system retries up to this many times, waiting _VIBE_REQUEST_RETRY_DELAY
# seconds between attempts, before passing whatever data it has to the agent.
_VIBE_REQUEST_MAX_RETRIES: int = 3
_VIBE_REQUEST_RETRY_DELAY: float = 15.0  # seconds between retry attempts


def run_vibe_report(report_id: str) -> str:
    """Execute all read-only SSH commands for *report_id* and return combined output.

    For ``last_trade``, ``trade_history_7d``, and ``pnl_snapshot``, the local
    control-panel SQLite is queried first (no SSH required) and its results are
    prepended to whatever the SSH commands return.

    Each command's output is separated by a section header.  Returns a message
    string (not empty) when SSH is not configured or all commands produce no output.
    """
    from openclaw_control import trade_log as _tl

    rid = report_id.lower()
    sections: list[str] = []

    # ── Local control-panel SQLite (no SSH required) ──────────────────────────
    if rid in ("last_trade", "trade_history_7d"):
        limit = 10 if rid == "last_trade" else 200
        rows = _tl.get_recent_trades(limit=limit)
        if rows:
            lines = [f"=== LOCAL TRADE LOG ({len(rows)} records, newest first) ==="]
            for t in rows:
                line = (
                    f"  {t['ts']}  {t['symbol']}  {t['side'].upper()}"
                    f"  size={t['size']}  price={t['fill_price']}"
                )
                if t.get("trade_id"):
                    line += f"  trade_id={t['trade_id']}"
                lines.append(line)
            sections.append("\n".join(lines))
        else:
            sections.append(
                "=== LOCAL TRADE LOG ===\n"
                "(empty — bot must POST trades to /trades/log on this server for them to appear here)"
            )
    elif rid == "pnl_snapshot":
        rows_pnl = _tl.get_recent_pnl(limit=24)
        if rows_pnl:
            lines = [f"=== LOCAL P&L LOG ({len(rows_pnl)} snapshots, newest first) ==="]
            for p in rows_pnl:
                kv = [p["ts"]]
                for k in ("total_pnl", "equity", "drawdown", "sharpe_ratio"):
                    if p.get(k) is not None:
                        kv.append(f"{k}={p[k]}")
                lines.append("  " + "  ".join(kv))
            sections.append("\n".join(lines))

    # ── SSH commands ──────────────────────────────────────────────────────────
    if not settings.ssh_readonly_host:
        return "\n\n".join(sections) if sections else "[OPENCLAW_SSH_READONLY_HOST not configured — read-only SSH probes are disabled]"

    commands = _report_commands_from_map().get(rid)
    if commands is None and not sections:
        valid = ", ".join(_report_commands_from_map().keys())
        return f"[Unknown report_id '{report_id}'. Valid ids: {valid}]"

    for cmd in (commands or []):
        result = run_ssh_readonly(cmd, timeout=120)
        if "error" in result:
            sections.append(f"--- {cmd[:60]}... ---\n[SSH error]")
            continue
        out = (result.get("stdout") or "").strip()
        err = (result.get("stderr") or "").strip()
        body = (out + ("\n" + err if err else "")).strip() or "(empty)"
        sections.append(f"--- {cmd[:60]}... ---\n{body}")
    return "\n\n".join(sections)


def _is_valid_report_data(report_data: str) -> bool:
    """Return True if *report_data* contains actual evidence worth storing in memory.

    Filters out empty responses, SSH error messages, and placeholder strings
    that would pollute the agent memory snapshot with useless content.
    """
    if len(report_data) <= 50:
        return False
    noise_markers = ("(empty)", "[SSH error]", "[SSH not configured", "[Unknown report_id", "[OPENCLAW_SSH_READONLY_HOST not configured")
    return not any(m in report_data for m in noise_markers)


# ── Vibe snapshot consolidation ───────────────────────────────────────────────

class _VibeSnapshot(NamedTuple):
    """Result of a consolidated Vibe snapshot collection."""
    snapshot: str                       # Full concatenated evidence text
    authoritative_evidence_source: str  # Label of the strongest probe
    evidence_summary: str               # ≤20 lines from the authoritative source
    any_usable: bool                    # True when at least one probe yielded real data


# Lines that carry no evidential value (probe fallback markers).
_NOISE_LINE_RE = _re.compile(
    r"^\s*(\[no [^\]]+\]"
    r"|\[(?:un|docker un)available\]"
    r"|\[SSH error\]"
    r"|\(empty\)"
    r"|)\s*$",
    _re.IGNORECASE,
)

# Keywords that indicate financially meaningful content.
_FINANCIAL_KW_RE = _re.compile(
    r"\b(pnl|trade|fill|filled|executed|order|profit|loss|equity"
    r"|sharpe|drawdown|return|net|gross|fee|slippage|signal)\b",
    _re.IGNORECASE,
)

# Source-type weight — higher = more authoritative for financial analysis.
_SOURCE_WEIGHTS: dict[str, int] = {
    "trade log files":          10,
    "performance analyser logs": 8,
    "trade/pnl log grep":        6,
    "log since window":          5,
    "log highlights":            3,
    "docker status":             2,
    "git log":                   1,
    "uptime":                    0,
}

# Shell grep pattern reused across secondary probes for consistency.
_FINANCIAL_GREP_PATTERN = "trade|pnl|net|gross|fee|slippage|profit|loss"


def _real_lines(content: str) -> list[str]:
    """Return lines from *content* that are not pure noise markers."""
    return [l for l in content.splitlines() if l.strip() and not _NOISE_LINE_RE.match(l)]


def _is_usable_probe_output(content: str) -> bool:
    """Return True if *content* contains at least one non-noise line."""
    return bool(_real_lines(content))


def _probe_strength(label: str, content: str) -> int:
    """Score a probe result for evidence ranking (higher = stronger)."""
    lines = _real_lines(content)
    if not lines:
        return 0
    base = len(lines)
    financial_bonus = 2 * sum(1 for l in lines if _FINANCIAL_KW_RE.search(l))
    source_bonus = _SOURCE_WEIGHTS.get(label, 0)
    return base + financial_bonus + source_bonus


def _consolidate_evidence(
    probe_results: list[tuple[str, str]],
) -> tuple[str, str, str, bool]:
    """Consolidate probe results into (snapshot, auth_source, evidence_summary, any_usable).

    Each element of *probe_results* is (label, content).
    Ranking: source weight + line count + financial keyword bonus.
    The authoritative source is the highest-ranked usable probe.
    Evidence summary is the first 20 meaningful lines of the authoritative content.
    """
    # Build full snapshot text (existing format kept intact)
    sections = [
        f"=== {label} ===\n{content}"
        for label, content in probe_results
        if content
    ]
    snapshot = "\n\n".join(sections).strip()

    # Rank probes; skip zero-score (no usable data)
    scored = [
        (label, content, _probe_strength(label, content))
        for label, content in probe_results
    ]
    scored.sort(key=lambda x: x[2], reverse=True)

    any_usable = scored[0][2] > 0 if scored else False

    if not any_usable:
        return snapshot, "", "", False

    auth_label, auth_content, _ = scored[0]
    summary_lines = _real_lines(auth_content)[:20]
    evidence_summary = "\n".join(summary_lines)

    return snapshot, auth_label, evidence_summary, True


def run_ssh(command: str, timeout: int = 10) -> dict:
    """Run *command* on the VIBE execution host (OPENCLAW_SSH_HOST).

    This is the *Vibe execution lane* — it targets settings.ssh_host and is
    used exclusively for mutative / execution operations (vibe runs, direct !
    commands).  Read-only probes must use :func:`run_ssh_readonly` instead.
    """
    try:
        proc = subprocess.run(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=5",
                settings.ssh_host,
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "type": "ssh",
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except Exception as e:
        return {
            "type": "ssh",
            "error": f"SSH error ({type(e).__name__}). Check server logs.",
        }


def run_ssh_readonly(command: str, timeout: int = 10) -> dict:
    """Run *command* on the read-only SSH host (OPENCLAW_SSH_READONLY_HOST).

    This is the *read-only lane* — it targets settings.ssh_readonly_host and
    is used for all probes, snapshots, autopilot evidence collection, terminal
    pills, and /ops/report.

    **No fallback**: when OPENCLAW_SSH_READONLY_HOST is unset this function
    returns an error dict and never falls back to the Vibe execution host.
    """
    if not settings.ssh_readonly_host:
        return {
            "type": "ssh_readonly",
            "error": (
                "OPENCLAW_SSH_READONLY_HOST is not configured — "
                "read-only SSH features are disabled. "
                "Set OPENCLAW_SSH_READONLY_HOST in .env to enable probes, "
                "snapshots, and terminal pills."
            ),
        }
    try:
        ssh_cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=5",
        ]
        if settings.ssh_readonly_key:
            ssh_cmd += ["-i", settings.ssh_readonly_key]
        ssh_cmd += [settings.ssh_readonly_host, command]
        proc = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "type": "ssh_readonly",
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except Exception as e:
        return {
            "type": "ssh_readonly",
            "error": f"SSH error ({type(e).__name__}). Check server logs.",
        }


def run_agent(text: str) -> dict:
    route = route_message(text)

    agent = controller if route.name == "ops" else analysis_agent
    session = _ops_session if route.name == "ops" else _analysis_session
    label = route.name

    if agent is None:
        return {
            "type": "agent",
            "agent": label,
            "error": "Agent not available (agents SDK not installed).",
        }

    def _call():
        prompt = (
            f"Host={settings.ssh_host}\n"
            f"Repo={settings.repo_dir}\n"
            f"{text}"
        )
        return Runner.run_sync(agent, prompt, session=session)

    future = EXECUTOR.submit(_call)

    try:
        result = future.result(timeout=25)
        return {
            "type": "agent",
            "agent": label,
            "output": result.final_output,
        }
    except FuturesTimeout:
        return {
            "type": "agent",
            "agent": label,
            "error": "AI timed out (25s)",
        }
    except Exception as e:
        return {
            "type": "agent",
            "agent": label,
            "error": f"Agent error ({type(e).__name__}). Check server logs.",
        }


_AGENT_REGISTRY = {
    "main":  (main_agent,    _main_session),
    "pnl":   (pnl_agent,     _pnl_session),
    "quant": (quant_agent,   _quant_session),
    "coo":   (coo_agent,     _coo_session),
    "vibe":  (vibe_planner,  _vibe_session),
}

# Per-agent max_turns limits.
# Main is unbounded (uses Runner default) — needs multiple turns for tool calls.
# Specialists (no tools) need only 1 turn; COO needs up to 3 (tool × 2 + final memo).
# Main can call ask_pnl + ask_quant + web_search + synthesis ≈ up to 8 turns.
_MAX_TURNS: dict[str, int | None] = {
    "main":  None,   # unbounded — tool calls need multiple turns
    "pnl":   1,
    "quant": 1,
    "coo":   3,
    "vibe":  1,      # planner: single pass, no tools
}

# Budget prefix injected into P&L / Quant prompts when the daily budget is low.
_BUDGET_LOW_PREFIX = "[BUDGET LOW] "

# Per-agent terminal_tail line caps for workspace injection.
# COO gets a tighter cap by default; keywords in the user message can override it.
_TERMINAL_TAIL_CAPS: dict[str, int] = {
    "main":  200,
    "pnl":   200,
    "quant": 200,
    "coo":   100,
    "vibe":  200,
}
_COO_DETAIL_KEYWORDS = frozenset(["detailed", "full review", "deep dive"])

# Outer timeout (seconds) per agent.  COO has a shorter soft budget so the
# fallback partial memo is delivered promptly instead of hanging until 25 s.
# Main has a generous budget because it may call ask_pnl + ask_quant +
# web_search + SSH probes in the same turn, each of which can take many seconds.
_AGENT_TIMEOUT: dict[str, int] = {
    "main": 300,
    "coo": 15,
}
_DEFAULT_TIMEOUT = 60

# ── Compound-request detection ────────────────────────────────────────────────
# Keywords that indicate a multi-step analysis task that benefits from Team
# Review rather than a single-agent response.
_COMPOUND_KEYWORDS: frozenset[str] = frozenset({
    "analyse", "analyze", "analysis",
    "strategy", "strategies",
    "backtest", "back-test", "back test",
    "test", "tests",
    "propose", "proposal",
    "assess", "assessment",
    "compare", "comparison",
    "history", "historical",
    "kraken",
    "review",
    "2 year", "2-year", "two year",
    "success rate", "win rate",
    "holding btc", "hold btc",
})


def _is_compound_request(text: str) -> bool:
    """Return True when *text* looks like a multi-agent analysis task.

    Triggers when ≥ 2 compound keywords appear in the (lower-cased) text,
    or when the message itself is longer than 300 characters.
    Any of these indicates a task too complex for a single-agent turn and
    better suited to Team Review orchestration.
    """
    lc = text.lower()
    if len(lc) > 300:
        return True
    hits = sum(1 for kw in _COMPOUND_KEYWORDS if kw in lc)
    return hits >= 2


def _coo_partial_memo(workspace: dict) -> str:
    """Return a partial COO decision memo when the agent times out."""
    ssh_target = settings.ssh_host or "(not configured)"
    repo_dir = settings.repo_dir or "(not configured)"
    tail = (workspace.get("terminal_tail") or "").strip()
    tail_lines = tail.splitlines()[-5:] if tail else []
    tail_snippet = "\n".join(tail_lines) if tail_lines else "(none)"
    return (
        "COO PARTIAL MEMO [timed out — 15 s budget exceeded]\n\n"
        "What we know:\n"
        f"- SSH target: {ssh_target}\n"
        f"- Repo dir: {repo_dir}\n"
        f"- Last terminal lines:\n{tail_snippet}\n\n"
        "What we don't know:\n"
        "- P&L and Quant sub-agent results (timed out before completion)\n\n"
        "Next actions (max 3):\n"
        "1. Use ⚡ Quick team review for a full parallel synthesis\n"
        "2. Re-send your prompt — COO context may have been too large\n"
        "3. Review terminal output for immediate signals\n\n"
        "For deeper synthesis, use the Team Review tab."
    )


def _record_run_usage(result: RunResult) -> None:
    """Extract token counts from a RunResult and update the daily budget tracker."""
    total_in = sum(r.usage.input_tokens for r in result.raw_responses if r.usage is not None)
    total_out = sum(r.usage.output_tokens for r in result.raw_responses if r.usage is not None)
    if total_in or total_out:
        budget.record_usage(total_in, total_out)


def handle_agent_message(agent_name: str, text: str, workspace: dict) -> dict:
    """Route a user message to the named agent, injecting shared workspace context."""
    name = (agent_name or "").strip().lower()
    entry = _AGENT_REGISTRY.get(name)
    if not entry:
        return {"agent": name, "error": f"Unknown agent: {name!r}"}

    agent, session = entry

    if agent is None:
        return {
            "agent": name,
            "error": "Agent not available (agents SDK not installed).",
        }

    # ── Auto-escalation to Team Review for compound analysis requests ─────────
    # When the user sends a complex multi-step analysis to the Main agent,
    # automatically start a Team Review orchestration run (which uses streaming
    # events, parallel sub-agents, and SSH evidence collection) rather than
    # attempting a single long-running Main agent turn that is likely to time out.
    if name == "main" and _is_compound_request(text):
        run_id = start_team_review("detailed", text, workspace)
        return {
            "agent": "main",
            "team_run_id": run_id,
            "output": (
                "🔀 Complex analysis detected — Team Review started automatically.\n"
                "Switching to Team tab… all agents (P&L, Quant, COO) will work on this in sequence."
            ),
        }

    # Per-agent terminal_tail cap; COO gets a larger cap if the user asks for detail.
    tail_cap = _TERMINAL_TAIL_CAPS.get(name, 200)
    if name == "coo" and any(kw in text.lower() for kw in _COO_DETAIL_KEYWORDS):
        tail_cap = 200

    # Build workspace context header (server-authoritative values + client terminal tail)
    ctx_lines = []
    if settings.ssh_host:
        ctx_lines.append(f"SSH target: {settings.ssh_host}")
    if settings.repo_dir:
        ctx_lines.append(f"Repo dir: {settings.repo_dir}")
    terminal_tail = (workspace.get("terminal_tail") or "").strip()
    if terminal_tail:
        tail_lines = terminal_tail.splitlines()[-tail_cap:]
        ctx_lines.append("Last terminal output:\n" + "\n".join(tail_lines))

    # Inject ops map core memory for all agents so they know where to look for data.
    ops_map_summary = _map_loader.get_summary()
    ctx_lines.append(ops_map_summary)

    # ------------------------------------------------------------------
    # Evidence-based memory injection (P&L, Quant, COO)
    # Load the persisted snapshot for this agent and append it as context.
    # If the environment fingerprint has changed (container restart, git HEAD
    # change, SSH target change) the snapshot is invalidated before injection.
    # ------------------------------------------------------------------
    if name in ("pnl", "quant", "coo"):
        snap, stored_fp = _memory.load_snapshot_with_fingerprint(name)
        if snap and stored_fp:
            # Quick fingerprint check using config-only fields (no SSH round-trip).
            current_fp_config_only = _memory.compute_fingerprint()
            # If the config-level component of the fingerprint changed, invalidate.
            # (Full fingerprint including container start times is updated on each
            # successful VIBE_REPORT_REQUEST execution below.)
            if not stored_fp.startswith(current_fp_config_only[:_FINGERPRINT_PREFIX_LEN]):
                _memory.invalidate(name, reason="SSH target or repo_dir changed")
                snap = {}
        if snap:
            # Summarise the snapshot for prompt injection; never inject raw blobs.
            mem_lines = ["\n=== AGENT MEMORY (evidence-based, auto-refreshed) ==="]
            for k, v in snap.items():
                if isinstance(v, str) and len(v) < _MAX_CONTEXT_STRING_LENGTH:
                    mem_lines.append(f"  [{k}] {v}")
                elif isinstance(v, (int, float, bool)):
                    mem_lines.append(f"  [{k}] {v}")
            mem_lines.append("=== END AGENT MEMORY ===")
            ctx_lines.append("\n".join(mem_lines))

        # COO also receives cached pnl and quant snapshots so it can answer
        # stateful questions from evidence collected during prior interactions
        # without always invoking sub-agent tools.
        if name == "coo":
            for sub_name in ("pnl", "quant"):
                sub_snap = _memory.load_snapshot(sub_name)
                if sub_snap:
                    sub_lines = [f"\n=== {sub_name.upper()} MEMORY (cached evidence) ==="]
                    for k, v in sub_snap.items():
                        if isinstance(v, str) and len(v) < _MAX_CONTEXT_STRING_LENGTH:
                            sub_lines.append(f"  [{k}] {v}")
                        elif isinstance(v, (int, float, bool)):
                            sub_lines.append(f"  [{k}] {v}")
                    sub_lines.append(f"=== END {sub_name.upper()} MEMORY ===")
                    ctx_lines.append("\n".join(sub_lines))

    ctx_header = "\n".join(ctx_lines)
    prompt = f"{ctx_header}\n\n{text}" if ctx_header else text

    # ------------------------------------------------------------------
    # Asymmetric budget enforcement
    # ------------------------------------------------------------------
    # COO refuses orchestration when budget is low.
    if name == "coo" and budget.is_low():
        return {
            "agent": name,
            "output": COO_BUDGET_MESSAGE,
        }

    # P&L and Quant receive a budget-low prefix so they return shortened outputs.
    if name in ("pnl", "quant") and budget.is_low():
        prompt = _BUDGET_LOW_PREFIX + prompt

    # Main agent: no budget-driven changes.

    max_turns = _MAX_TURNS.get(name)
    kwargs: dict = {"session": session}
    if max_turns is not None:
        kwargs["max_turns"] = max_turns

    timeout_s = _AGENT_TIMEOUT.get(name, _DEFAULT_TIMEOUT)

    def _call(p: str):
        return Runner.run_sync(agent, p, **kwargs)

    future = EXECUTOR.submit(_call, prompt)
    try:
        result = future.result(timeout=timeout_s)
        _record_run_usage(result)
        final_output = result.final_output

        # ------------------------------------------------------------------
        # VIBE_REPORT_REQUEST intercept (main, pnl, quant agents)
        # If the agent emits VIBE_REPORT_REQUEST: <report_id>, execute the
        # corresponding read-only SSH probes and re-run the agent once with
        # the results injected.  This gives tool-free agents access to live
        # VPS data without asking the operator to paste logs.
        # After a successful probe, save a derived summary to agent memory
        # so subsequent calls don't need to re-probe for the same data.
        #
        # Patience: if the first probe returns empty (the remote command may
        # still be running, e.g. a long backtest), we retry up to
        # _VIBE_REQUEST_MAX_RETRIES times with _VIBE_REQUEST_RETRY_DELAY s
        # between attempts before giving up and returning whatever data we have.
        # ------------------------------------------------------------------
        if name in ("main", "pnl", "quant") and settings.ssh_readonly_host:
            match = _VIBE_REQUEST_RE.search(final_output)
            if match:
                report_id = match.group(1).lower()
                # Validate against the known set before executing any SSH commands.
                _known_ids = set(_report_commands_from_map().keys())
                if report_id in _known_ids:
                    # ── Retry loop: wait for data with patience ────────────
                    report_data = ""
                    for _attempt in range(_VIBE_REQUEST_MAX_RETRIES):
                        report_data = run_vibe_report(report_id)
                        if _is_valid_report_data(report_data):
                            break
                        if _attempt < _VIBE_REQUEST_MAX_RETRIES - 1:
                            _time.sleep(_VIBE_REQUEST_RETRY_DELAY)
                    augmented_prompt = (
                        f"{ctx_header}\n\n{text}\n\n"
                        f"=== VIBE REPORT: {report_id} ===\n{report_data}\n"
                        f"=== END VIBE REPORT ===\n"
                        f"Now answer the original question using the report data above."
                    )
                    # One follow-up pass; reuse the same session for continuity.
                    fut2 = EXECUTOR.submit(_call, augmented_prompt)
                    try:
                        result2 = fut2.result(timeout=timeout_s)
                        _record_run_usage(result2)
                        final_output = result2.final_output
                        # ── Persist evidence to memory (derived summary only) ──
                        # Only store if the report returned actual data, not just
                        # "(empty)" or SSH error lines.
                        if name in ("pnl", "quant") and _is_valid_report_data(report_data):
                            ct, gh = _vibe_reports.extract_fingerprint_fields(report_data)
                            fp = _memory.compute_fingerprint(ct, gh)
                            existing_snap = _memory.load_snapshot(name)
                            existing_snap[f"last_{report_id}"] = report_data[:_MAX_REPORT_SUMMARY_LENGTH]
                            existing_snap[f"last_{report_id}_at"] = datetime.now(_tz.utc).replace(microsecond=0).isoformat()
                            existing_snap["last_report_id"] = report_id
                            _memory.save_snapshot(name, existing_snap, fp)
                    except (FuturesTimeout, Exception):
                        pass  # fall back to the original output that had the request

        # ── Persist COO derived-summary memory ────────────────────────────────
        # Save a compact memo summary after each substantive COO response.
        # The fingerprint is config-only (SSH host + repo dir) because COO does
        # not run SSH probes directly; it is invalidated on the next load if
        # ssh_host or repo_dir change.  Evidence-anchoring is indirect: COO's
        # context always includes pnl/quant cached snapshots (above), so its
        # analysis is grounded in previously verified evidence.
        # Only save when the output is substantive and not a partial/error memo.
        if name == "coo" and len(final_output) > 100 and not final_output.startswith("["):
            _fp = _memory.compute_fingerprint()
            coo_snap = _memory.load_snapshot("coo")
            coo_snap["last_coo_at"] = datetime.now(_tz.utc).replace(microsecond=0).isoformat()
            coo_snap["last_memo_summary"] = final_output[:200]
            coo_snap["halt_flagged"] = "halt" in final_output.lower()
            _memory.save_snapshot("coo", coo_snap, _fp)

        return {"agent": name, "output": final_output}
    except FuturesTimeout:
        if name == "coo":
            return {"agent": name, "output": _coo_partial_memo(workspace)}
        return {"agent": name, "error": f"Agent timed out after {timeout_s} s. Please try again."}
    except Exception as e:
        # Return only the exception type to avoid leaking internal stack traces
        return {"agent": name, "error": f"Agent error ({type(e).__name__}). Check server logs."}


def handle_message(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"type": "noop"}

    if text.startswith("!"):
        return run_ssh(text[1:].strip())

    return run_agent(text)


# ── Team-review orchestration ─────────────────────────────────────────────────

_TEAM_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_atexit.register(_TEAM_EXECUTOR.shutdown, wait=False)
_TEAM_RUNS: dict[str, dict] = {}
_TEAM_RUNS_LOCK = _threading.Lock()

DEFAULT_TEAM_PROMPT = (
    "Review the latest workspace context and produce: "
    "(1) P&L summary including any halt-state impact, (2) quant critique including halt trigger "
    "analysis if applicable, (3) COO decision memo with 3 next actions max "
    "and optionally one /copilot task."
)

# COO synthesis agent for team review — reports are injected into the prompt;
# no sub-agent tool calls are made so there is no recursion risk.
# Only constructed when the agents SDK is available.
if _AGENTS_SDK_AVAILABLE:
    _team_coo = Agent(
        name="COO Synthesis",
        instructions=(
            "You are the OpenClaw COO. Your team's P&L and Quant reports are already provided "
            "in the prompt — do NOT call any tools.\n"
            "Produce a concise decision memo:\n"
            "1) What we know / what we don't know\n"
            "2) Risks & constraints\n"
            "3) Next actions (max 3 items)\n"
            "4) If a code change is justified: one /copilot task sentence with acceptance criteria\n"
            "Always end your response with 'Next actions (max 3)' and the numbered list.\n"
            "Do NOT execute SSH commands or suggest destructive actions.\n"
        ),
        model_settings=ModelSettings(max_tokens=800),
        tools=[],
    )
else:
    _team_coo = None  # type: ignore[assignment]


def _now_iso() -> str:
    return datetime.now(_tz.utc).replace(microsecond=0).isoformat()


def _push_event(run: dict, agent: str, etype: str, content: str, **extra) -> None:
    ev = {"t": _now_iso(), "agent": agent, "type": etype, "content": content}
    if extra:
        ev.update(extra)
    with run["lock"]:
        run["events"].append(ev)


def _start_heartbeat(run: dict, deadline: float, interval: float = 30.0) -> None:
    """Spawn a daemon thread that emits system heartbeat events every *interval* s.

    The thread stops automatically when the orchestration deadline passes or
    run["done"] becomes True.  Heartbeats keep the frontend feed visibly alive
    during long LLM waits or slow SSH probes so the operator knows work is
    in progress.
    """
    def _beat() -> None:
        while True:
            _time.sleep(interval)
            with run["lock"]:
                done = run["done"]
            if done or _time.monotonic() >= deadline:
                return
            rem = max(0, int(deadline - _time.monotonic()))
            _push_event(run, "system", "heartbeat", f"⏳ Still working… ({rem}s remaining)")

    _threading.Thread(target=_beat, daemon=True).start()


def _run_ssh_with_heartbeat(run: dict, label: str, cmd: str, timeout_s: float) -> dict:
    """Run a read-only SSH probe, emitting progress heartbeats every 15 s.

    Long-running SSH commands (e.g. backtests, 2-year history pulls) block for
    up to *timeout_s* seconds.  A background thread emits vibe-feed events every
    15 s so the team feed shows that the probe is still running rather than
    appearing frozen.  SSH timeout is capped at 300 s regardless of *timeout_s*.
    """
    capped_timeout = int(min(timeout_s, 300))
    stop = _threading.Event()

    def _narrate() -> None:
        elapsed = 0
        while not stop.wait(15):
            elapsed += 15
            _push_event(run, "vibe", "message", f"  ↳ ⏳ {label} still running… ({elapsed}s elapsed)")

    t = _threading.Thread(target=_narrate, daemon=True)
    t.start()
    try:
        return run_ssh_readonly(cmd, timeout=capped_timeout)
    finally:
        stop.set()


def _build_team_ctx(workspace: dict) -> str:
    lines: list[str] = []
    if settings.ssh_host:
        lines.append(f"SSH target: {settings.ssh_host}")
    if settings.repo_dir:
        lines.append(f"Repo dir: {settings.repo_dir}")
    review_period = (workspace.get("review_period") or "").strip()
    if review_period:
        lines.append(f"Review period requested: {review_period}")
    conv = workspace.get("conversation_history") or []
    if conv:
        # Use last 10 messages in chronological order, each capped at 400 chars.
        # The frontend may send up to 30; the backend constrains to a tighter window
        # so the context string stays within reasonable token limits for the agents.
        conv_lines = ["Conversation context:"]
        for item in conv[-10:]:
            role = "User" if item.get("role") == "user" else "Agent"
            text = (item.get("text") or "").strip()[:400]
            if text:
                conv_lines.append(f"[{role}] {text}")
        lines.append("\n".join(conv_lines))
    tail = (workspace.get("terminal_tail") or "").strip()
    if tail:
        capped = "\n".join(tail.splitlines()[-200:])
        lines.append(f"Last terminal output:\n{capped}")
    # Inject ops map core memory so all team-review agents know data locations.
    lines.append(_map_loader.get_summary())
    return "\n".join(lines)


def _gather_vibe_snapshot(run: dict, timeout_s: float) -> _VibeSnapshot:
    """Probe the server with primary + secondary read-only SSH probes.

    Primary probes (status, highlights, file discovery) and secondary probes
    (trade/pnl log grep, --since window, performance_analyser logs) are always
    attempted within the available time budget.

    Each probe streams its label and result snippet into the 'vibe' feed.
    Returns a consolidated _VibeSnapshot with the authoritative evidence source
    and a ≤20-line evidence summary derived from the strongest probe result.
    """
    if not settings.ssh_readonly_host:
        _push_event(run, "vibe", "message", "READONLY SSH not configured — snapshot skipped")
        return _VibeSnapshot("", "", "", False)

    _push_event(run, "vibe", "start", "")
    t_deadline = _time.monotonic() + timeout_s
    repo = settings.repo_dir or "/opt/openclaw-crypto"

    # ── Primary probes: status + highlights + file discovery ─────────────────
    PRIMARY_PROBES: list[tuple[str, str, int]] = [
        (
            "uptime",
            "uptime 2>/dev/null || echo '[unavailable]'",
            4,
        ),
        (
            "docker status",
            "docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.State}}' "
            "2>/dev/null || echo '[docker unavailable]'",
            5,
        ),
        (
            "log highlights",
            "(docker logs --tail=30 openclaw-orchestrator 2>&1 | "
            "grep -iE 'HALT|error|pnl|trade|signal|warning' | tail -n 20) "
            "2>/dev/null || echo '[no matches]'",
            7,
        ),
        (
            "git log",
            f"cd {repo} && git log -n 5 --oneline 2>/dev/null || echo '[unavailable]'",
            5,
        ),
        (
            "trade log files",
            f"find {repo} -maxdepth 5 \\( -name 'trade*.csv' -o -name '*trades*.csv' "
            f"-o -name 'pnl*.csv' -o -name '*pnl*.csv' \\) 2>/dev/null | head -n 6 "
            f"| xargs -r tail -n 25 2>/dev/null || echo '[no trade log files found]'",
            6,
        ),
    ]

    # ── Secondary probes: trade/pnl log grep + --since window ────────────────
    SECONDARY_PROBES: list[tuple[str, str, int]] = [
        (
            "trade/pnl log grep",
            f"(docker logs --tail=100 openclaw-orchestrator 2>&1 | "
            f"grep -iE '{_FINANCIAL_GREP_PATTERN}' | tail -n 30) "
            "2>/dev/null || echo '[no trade/pnl matches]'",
            7,
        ),
        (
            "performance analyser logs",
            "(docker logs --tail=200 performance_analyser 2>&1 | "
            "grep -iE 'pnl|trade|return|sharpe|equity|profit|loss' | tail -n 30) "
            "2>/dev/null || echo '[no matches]'",
            7,
        ),
        (
            "log since window",
            f"(docker logs --since 48h openclaw-orchestrator 2>&1 | "
            f"grep -iE '{_FINANCIAL_GREP_PATTERN}' | tail -n 30) "
            "2>/dev/null || echo '[no matches]'",
            8,
        ),
    ]

    probe_results: list[tuple[str, str]] = []

    # ── Per-run probe loop guard ──────────────────────────────────────────────
    # Initialise the per-run probe counter the first time _gather_vibe_snapshot
    # is called for this run.  Subsequent calls (e.g. from a retry path) share
    # the same counter so a probe that has already run 3 times is skipped.
    with run["lock"]:
        if "probe_counts" not in run:
            run["probe_counts"] = _Counter()
    # Snapshot the counter reference outside the lock for the loop below.
    probe_counts: _Counter = run["probe_counts"]

    for label, cmd, t in PRIMARY_PROBES + SECONDARY_PROBES:
        if _time.monotonic() >= t_deadline:
            _push_event(run, "vibe", "message", "⏱ Time budget low — remaining probes skipped")
            break
        # Loop guard: skip probes that have already run the maximum number of times.
        with run["lock"]:
            count = probe_counts[label]
        if count >= 3:
            _push_event(run, "vibe", "message", f"🔁 Loop guard — {label} already ran {count}× this run, skipping")
            probe_results.append((label, ""))
            continue
        with run["lock"]:
            probe_counts[label] += 1
        _push_event(run, "vibe", "message", f"📡 {label}…")
        # Use heartbeat wrapper so the feed stays alive during slow SSH calls.
        remaining = max(1.0, t_deadline - _time.monotonic())
        probe_timeout = min(t, remaining)
        r = _run_ssh_with_heartbeat(run, label, cmd, probe_timeout)
        if "error" in r:
            _push_event(run, "vibe", "message", "  ↳ [SSH error]")
            probe_results.append((label, ""))
            continue
        out = (r.get("stdout") or "").strip()
        if out:
            snippet = "\n".join(out.splitlines()[:12])
            _push_event(run, "vibe", "message", f"  ↳ {snippet}")
            probe_results.append((label, out))
        else:
            _push_event(run, "vibe", "message", "  ↳ (no output)")
            probe_results.append((label, ""))

    _push_event(run, "vibe", "done", "")

    snapshot, auth_source, evidence_summary, any_usable = _consolidate_evidence(probe_results)

    if auth_source:
        _push_event(
            run, "vibe", "message",
            f"✅ Authoritative evidence source: {auth_source}",
        )

    return _VibeSnapshot(snapshot, auth_source, evidence_summary, any_usable)


def _run_for_team(agent, prompt: str, timeout_s: float) -> tuple[str, bool]:
    """Submit agent to the team executor (single pass, no session). Returns (output, is_error).

    is_error is True for both timeouts and exceptions so callers can use a single flag
    to distinguish successful agent output from failure sentinels.
    """
    if agent is None:
        return "[Agent not available — agents SDK not installed]", True
    fut = _TEAM_EXECUTOR.submit(Runner.run_sync, agent, prompt, max_turns=1)
    try:
        res = fut.result(timeout=max(1.0, timeout_s))
        _record_run_usage(res)
        return res.final_output, False
    except FuturesTimeout:
        # cancel() is a no-op on already-running futures; the thread will finish naturally.
        return f"[{agent.name} timed out]", True
    except Exception as exc:
        return f"[{agent.name} error: {type(exc).__name__}]", True


def _make_agent_prompt(user_prompt: str, ctx: str, bprefix: str = "") -> str:
    body = f"{ctx}\n\n{user_prompt}" if ctx else user_prompt
    return (bprefix + body) if bprefix else body


def _push_agent_result(run: dict, agent_key: str, output: str, is_error: bool, label: str = "") -> None:
    """Push events for a completed _run_for_team call.

    Emits an 'error' event when is_error is True (timeout or exception),
    otherwise emits a 'message' + 'done' pair so the feed reflects the phase gate.
    """
    if is_error:
        _push_event(run, agent_key, "error", output)
    else:
        _push_event(run, agent_key, "message", output)
        _push_event(run, agent_key, "done", label)


def _evidence_gate(run: dict, vs: _VibeSnapshot) -> bool:
    """Return True if downstream agents may proceed; False (with explicit failure event) if not.

    Proceeds if *any* probe yielded usable evidence (secondary probes can override
    empty primary probes).  Fails only when SSH is configured but ALL probes are
    empty or errored, to prevent hallucinated analysis.
    When SSH is not configured the user is working offline; agents may still use
    whatever workspace context was provided (terminal tail, conversation history).
    """
    if settings.ssh_readonly_host and not vs.any_usable:
        _push_event(
            run, "vibe", "error",
            "❌ Evidence collection failed — SSH is configured but all probes returned no data.\n"
            "Review cannot proceed without live server evidence to avoid hallucinated analysis.\n"
            "Check SSH connectivity and retry.",
        )
        return False
    return True


def _build_enhanced_ctx(ctx: str, vs: _VibeSnapshot) -> str:
    """Build the agent context string from base *ctx* and a consolidated *_VibeSnapshot*.

    Always includes the full snapshot text so agents have all raw evidence.
    Appends a dedicated Evidence Summary block (authoritative source + ≤20 key lines)
    so P&L / Quant / COO prompts know which source to cite.
    """
    if not vs.snapshot:
        return ctx
    parts = [ctx, "=== Live Server Snapshot ===", vs.snapshot]
    if vs.authoritative_evidence_source and vs.evidence_summary:
        parts.append(
            f"=== Evidence Summary (authoritative: {vs.authoritative_evidence_source}) ===\n"
            f"{vs.evidence_summary}"
        )
    return "\n\n".join(parts)


def _run_ew(agent, prompt: str, run: dict, agent_key: str, timeout_s: float) -> tuple[str, bool]:
    """Thin wrapper around run_with_evidence pre-bound to team-review dependencies."""

    def _on_probe_success(report_id: str, probe_data: str) -> None:
        """Persist a derived summary to memory after a successful team-review Vibe probe."""
        if agent_key not in _AGENTS_WITH_PROBE_PERSISTENCE or not _is_valid_report_data(probe_data):
            return
        ct, gh = _vibe_reports.extract_fingerprint_fields(probe_data)
        fp = _memory.compute_fingerprint(ct, gh)
        snap = _memory.load_snapshot(agent_key)
        snap[f"last_{report_id}"] = probe_data[:_MAX_REPORT_SUMMARY_LENGTH]
        snap[f"last_{report_id}_at"] = datetime.now(_tz.utc).replace(microsecond=0).isoformat()
        snap["last_report_id"] = report_id
        _memory.save_snapshot(agent_key, snap, fp)

    return _run_with_evidence(
        agent, prompt, run,
        push_event=_push_event,
        run_for_team=_run_for_team,
        run_vibe_probe=run_vibe_report,
        known_report_ids=set(_report_commands_from_map().keys()),
        timeout_s=timeout_s,
        ssh_configured=bool(settings.ssh_readonly_host),
        agent_key=agent_key,
        on_probe_success=_on_probe_success,
    )


# ── Proposal deduplication (24-hour fingerprint cache) ────────────────────────
# Maps proposal fingerprint → wall-clock expiry (epoch seconds).
# Entries are lazily evicted when the cache grows large.

_PROPOSAL_FPS: dict[str, float] = {}
_PROPOSAL_FPS_TTL: float = 86400.0   # 24 hours


def _seen_proposal_recently(fp: str) -> bool:
    """Return True if *fp* is still within its 24 h TTL window."""
    exp = _PROPOSAL_FPS.get(fp)
    return exp is not None and _time.time() < exp


def _record_proposal_fp(fp: str) -> None:
    """Record *fp* with a 24 h expiry; evict stale entries when cache grows large."""
    if len(_PROPOSAL_FPS) > 500:
        now = _time.time()
        stale = [k for k, v in _PROPOSAL_FPS.items() if v <= now]
        for k in stale:
            del _PROPOSAL_FPS[k]
    _PROPOSAL_FPS[fp] = _time.time() + _PROPOSAL_FPS_TTL


def _dispatch_team_action(coo_output: str, run: dict) -> None:
    """Dispatch COO next-action proposals to the team feed when budget allows.

    Passes ``github_repo=None`` so that ``dispatch_coo_action`` derives
    candidate repos from the memo content rather than defaulting to
    ``settings.github_repo`` (which would silently guess openclaw-control).
    """
    if budget.is_low():
        return
    seen = {fp for fp in _PROPOSAL_FPS if _seen_proposal_recently(fp)}
    emitted = _dispatch_coo_action(
        coo_output, run,
        push_event=_push_event,
        github_repo=None,           # content-driven; do not pre-select
        allowed_repos=_GH_ALLOWED_REPOS,
        seen_fps=seen,
    )
    for fp in emitted:
        _record_proposal_fp(fp)


def _orchestrate_quick(run: dict, user_prompt: str, ctx: str, deadline: float) -> None:
    # Start heartbeat so the feed stays alive during long LLM or SSH waits.
    _start_heartbeat(run, deadline)

    # Narrate exactly what context was assembled
    ctx_sources = []
    if "Conversation context:" in ctx:
        ctx_sources.append("conversation history")
    if "Last terminal output:" in ctx:
        ctx_sources.append("terminal output")
    if "SSH target:" in ctx:
        ctx_sources.append("SSH workspace")
    if "OPS MAP CORE MEMORY" in ctx:
        ctx_sources.append("ops map")
    sources_str = " + ".join(ctx_sources) if ctx_sources else "workspace config"
    _push_event(
        run, "main", "message",
        f"📋 Context sources: {sources_str}\n"
        f"🎯 Objective: {user_prompt[:300]}",
    )

    # Phase 1: Vibe — collect live server evidence before any agent runs
    rem = deadline - _time.monotonic()
    vs = _VibeSnapshot("", "", "", False)
    if rem > 12:
        vs = _gather_vibe_snapshot(run, min(10.0, rem - 10))
    else:
        _push_event(run, "vibe", "message", "⏱ Skipped — time budget low")

    # Evidence gate — block downstream agents if SSH is configured but all probes failed
    if not _evidence_gate(run, vs):
        return

    enhanced_ctx = _build_enhanced_ctx(ctx, vs)

    bprefix = _BUDGET_LOW_PREFIX if budget.is_low() else ""
    pnl_prompt = _make_agent_prompt(user_prompt, enhanced_ctx, bprefix)
    quant_prompt = _make_agent_prompt(user_prompt, enhanced_ctx, bprefix)

    # Phase 2: P&L — sequential; Quant does not start until P&L completes
    _push_event(run, "pnl", "start", "")
    pnl_out, pnl_to = _run_ew(pnl_agent, pnl_prompt, run, "pnl", max(1.0, deadline - _time.monotonic()))
    _push_agent_result(run, "pnl", pnl_out, pnl_to)

    # Phase 3: Quant — gates on P&L completing
    rem = deadline - _time.monotonic()
    if rem < 3:
        _push_event(run, "quant", "error", "[Quant skipped — time budget exhausted after P&L]")
        return
    _push_event(run, "quant", "start", "")
    quant_out, quant_to = _run_ew(quant_agent, quant_prompt, run, "quant", max(1.0, deadline - _time.monotonic()))
    _push_agent_result(run, "quant", quant_out, quant_to)

    # Phase 4: COO synthesis — gates on Quant completing
    rem = deadline - _time.monotonic()
    if rem < 3:
        _push_event(run, "coo", "error", "[COO skipped — time budget exhausted]")
        return

    coo_prompt = (
        f"{enhanced_ctx}\n\n=== P&L Report ===\n{pnl_out}\n\n"
        f"=== Quant Report ===\n{quant_out}\n\n"
        f"=== Directive ===\n{user_prompt}"
    )
    _push_event(run, "coo", "start", "")
    coo_out, _ = _run_for_team(_team_coo, coo_prompt, timeout_s=rem)
    _push_event(run, "coo", "message", coo_out)
    _push_event(run, "coo", "done", "")
    _dispatch_team_action(coo_out, run)


def _orchestrate_detailed(run: dict, user_prompt: str, ctx: str, deadline: float) -> None:
    # Start heartbeat so the feed stays alive during long LLM or SSH waits.
    _start_heartbeat(run, deadline)

    # Narrate exactly what context was assembled
    ctx_sources = []
    if "Conversation context:" in ctx:
        ctx_sources.append("conversation history")
    if "Last terminal output:" in ctx:
        ctx_sources.append("terminal output")
    if "SSH target:" in ctx:
        ctx_sources.append("SSH workspace")
    if "OPS MAP CORE MEMORY" in ctx:
        ctx_sources.append("ops map")
    sources_str = " + ".join(ctx_sources) if ctx_sources else "workspace config"
    _push_event(
        run, "main", "message",
        f"📋 Context sources: {sources_str}\n"
        f"🎯 Objective: {user_prompt[:300]}",
    )

    # Phase 1: Vibe — collect live server evidence before any agent runs
    rem = deadline - _time.monotonic()
    vs = _VibeSnapshot("", "", "", False)
    if rem > 18:
        vs = _gather_vibe_snapshot(run, min(14.0, rem - 14))
    else:
        _push_event(run, "vibe", "message", "⏱ Skipped — time budget low")

    # Evidence gate — block downstream agents if SSH is configured but all probes failed
    if not _evidence_gate(run, vs):
        return

    enhanced_ctx = _build_enhanced_ctx(ctx, vs)

    bprefix = _BUDGET_LOW_PREFIX if budget.is_low() else ""

    # ── Round 1 ───────────────────────────────────────────────────────────────
    _push_event(run, "system", "message", "── Round 1 ──")

    pnl_prompt = _make_agent_prompt(user_prompt, enhanced_ctx, bprefix)
    quant_prompt = _make_agent_prompt(user_prompt, enhanced_ctx, bprefix)

    # Phase 2a: P&L Round 1 — sequential gate
    _push_event(run, "pnl", "start", "Round 1")
    pnl_r1, pnl_r1_to = _run_ew(pnl_agent, pnl_prompt, run, "pnl", max(1.0, deadline - _time.monotonic()))
    _push_agent_result(run, "pnl", pnl_r1, pnl_r1_to, "Round 1")

    # Phase 2b: Quant Round 1 — gates on P&L Round 1 completing
    rem = deadline - _time.monotonic()
    if rem < 3:
        _push_event(run, "quant", "error", "[Quant R1 skipped — time budget exhausted after P&L R1]")
        return
    _push_event(run, "quant", "start", "Round 1")
    quant_r1, quant_r1_to = _run_ew(quant_agent, quant_prompt, run, "quant", max(1.0, deadline - _time.monotonic()))
    _push_agent_result(run, "quant", quant_r1, quant_r1_to, "Round 1")

    # Phase 2c: COO Round 1 — gates on Quant Round 1 completing
    rem = deadline - _time.monotonic()
    if rem < 3:
        _push_event(run, "coo", "error", "[COO R1 skipped — time budget exhausted]")
        return

    coo_r1_prompt = (
        f"{enhanced_ctx}\n\n=== P&L Report (Round 1) ===\n{pnl_r1}\n\n"
        f"=== Quant Report (Round 1) ===\n{quant_r1}\n\n"
        f"=== Directive ===\n{user_prompt}"
    )
    _push_event(run, "coo", "start", "Round 1")
    coo_r1, r1_is_error = _run_for_team(_team_coo, coo_r1_prompt, timeout_s=rem)
    _push_event(run, "coo", "message", coo_r1)
    _push_event(run, "coo", "done", "Round 1")

    # ── Round 2 (if time permits) ─────────────────────────────────────────────
    rem = deadline - _time.monotonic()
    if rem < 6 or r1_is_error:
        _push_event(run, "system", "message", "Round 2 skipped — time budget exhausted")
        return

    _push_event(run, "system", "message", "── Round 2 ──")

    revise_directive = (
        f"Revise your analysis based on the COO's Round 1 memo:\n"
        f"=== COO Round 1 Memo ===\n{coo_r1}\n\n"
        f"=== Original directive ===\n{user_prompt}"
    )
    pnl_r2_prompt = _make_agent_prompt(revise_directive, enhanced_ctx, bprefix)
    quant_r2_prompt = _make_agent_prompt(revise_directive, enhanced_ctx, bprefix)

    # Phase 3a: P&L Round 2 — sequential gate
    _push_event(run, "pnl", "start", "Round 2")
    pnl_r2, pnl_r2_to = _run_ew(pnl_agent, pnl_r2_prompt, run, "pnl", max(1.0, deadline - _time.monotonic()))
    _push_agent_result(run, "pnl", pnl_r2, pnl_r2_to, "Round 2")

    # Phase 3b: Quant Round 2 — gates on P&L Round 2 completing
    rem = deadline - _time.monotonic()
    if rem < 3:
        _push_event(run, "quant", "error", "[Quant R2 skipped — time budget exhausted after P&L R2]")
        return
    _push_event(run, "quant", "start", "Round 2")
    quant_r2, quant_r2_to = _run_ew(quant_agent, quant_r2_prompt, run, "quant", max(1.0, deadline - _time.monotonic()))
    _push_agent_result(run, "quant", quant_r2, quant_r2_to, "Round 2")

    # Phase 3c: COO final — gates on Quant Round 2 completing
    rem = deadline - _time.monotonic()
    if rem < 3:
        _push_event(run, "coo", "error", "[COO final skipped — time budget exhausted]")
        return

    coo_final_prompt = (
        f"{enhanced_ctx}\n\n=== P&L (Round 1) ===\n{pnl_r1}\n\n"
        f"=== Quant (Round 1) ===\n{quant_r1}\n\n"
        f"=== COO Round 1 Memo ===\n{coo_r1}\n\n"
        f"=== P&L (Round 2, revised) ===\n{pnl_r2}\n\n"
        f"=== Quant (Round 2, revised) ===\n{quant_r2}\n\n"
        f"=== Directive ===\n{user_prompt}\n\n"
        "Produce your FINAL decision memo incorporating all rounds."
    )
    _push_event(run, "coo", "start", "Round 2 (final)")
    coo_final, _ = _run_for_team(_team_coo, coo_final_prompt, timeout_s=rem)
    _push_event(run, "coo", "message", coo_final)
    _push_event(run, "coo", "done", "Round 2 (final)")
    _dispatch_team_action(coo_final, run)



def start_team_review(mode: str, prompt: str, workspace: dict) -> str:
    """Start a team review orchestration run in a background thread. Returns run_id."""
    run_id = _uuid.uuid4().hex[:8]
    run: dict = {"events": [], "done": False, "lock": _threading.Lock()}
    with _TEAM_RUNS_LOCK:
        _TEAM_RUNS[run_id] = run

    # Sequential execution (Vibe → P&L → Quant → COO) requires more wall-clock time
    # than the former parallel P&L+Quant approach.  Budgets are raised to allow
    # long-running SSH operations (e.g. 2-year backtests) to complete:
    # quick:    180 s — Vibe(20) + P&L(50) + Quant(50) + COO(40) + slack(20)
    # detailed: 540 s (9 min) — 2 full rounds with generous headroom for slow SSH
    timeout = 180 if mode == "quick" else 540

    # Determine user prompt: explicit prompt > last user message from conversation > default
    user_prompt = (prompt or "").strip()
    if not user_prompt:
        conv = workspace.get("conversation_history") or []
        for item in reversed(conv):
            if item.get("role") == "user":
                user_prompt = (item.get("text") or "").strip()[:500]
                break
    user_prompt = user_prompt or DEFAULT_TEAM_PROMPT

    ctx = _build_team_ctx(workspace)

    # Narrate context assembly into the feed before the orchestration thread starts
    conv = workspace.get("conversation_history") or []
    conv_count = sum(1 for m in conv if m.get("role") == "user")
    ctx_note_parts = [f"mode={mode}"]
    if conv_count:
        ctx_note_parts.append(
            f"building context string from {conv_count} user message(s) "
            f"— last 10 in chronological order, each truncated to 400 chars"
        )
    if workspace.get("terminal_tail"):
        ctx_note_parts.append("terminal tail included")
    _push_event(run, "system", "run-start", " · ".join(ctx_note_parts))

    def _orchestrate() -> None:
        deadline = _time.monotonic() + timeout
        try:
            if mode == "quick":
                _orchestrate_quick(run, user_prompt, ctx, deadline)
            else:
                _orchestrate_detailed(run, user_prompt, ctx, deadline)
        except Exception as exc:
            _push_event(run, "system", "error", f"Orchestration error: {type(exc).__name__}")
        finally:
            with run["lock"]:
                run["done"] = True

    _threading.Thread(target=_orchestrate, daemon=True).start()
    return run_id


def get_team_review_events(run_id: str, cursor: int = 0) -> dict:
    """Return new events since cursor position, and whether the run is done."""
    run = _TEAM_RUNS.get(run_id)
    if run is None:
        return {"events": [], "done": False, "error": "run_not_found"}
    with run["lock"]:
        events = list(run["events"][cursor:])
        done = run["done"]
    return {"events": events, "done": done}


# ── Vibe execution gateway ────────────────────────────────────────────────────

_VIBE_RUNS: dict[str, dict] = {}
_VIBE_RUNS_LOCK = _threading.Lock()


_VIBE_TIMEOUT = 900  # seconds — vibe runs can take up to 15 minutes


def start_vibe_run(workdir: str, prompt: str) -> str:
    """Run ``vibe --workdir <workdir> --prompt <prompt>`` on the VPS via SSH.

    Constructs the remote command from *workdir* and *prompt* using
    ``shlex.quote`` to prevent shell injection.  Runs in a daemon thread and
    returns a run_id that callers can poll with :func:`get_vibe_run`.
    """
    vibe_command = (
        f"vibe --workdir {_shlex.quote(workdir)} --prompt {_shlex.quote(prompt)}"
    )
    run_id = _uuid.uuid4().hex[:8]
    run: dict = {"status": "running", "output": "", "error": ""}
    with _VIBE_RUNS_LOCK:
        _VIBE_RUNS[run_id] = run

    def _execute() -> None:
        if not settings.ssh_host:
            with _VIBE_RUNS_LOCK:
                run["status"] = "error"
                run["error"] = (
                    "OPENCLAW_SSH_HOST is not configured. "
                    "Set it in .env to enable remote Vibe execution."
                )
            return
        result = run_ssh(vibe_command, timeout=_VIBE_TIMEOUT)
        with _VIBE_RUNS_LOCK:
            if "error" in result:
                run["status"] = "error"
                run["error"] = result["error"]
            else:
                run["status"] = "done"
                run["output"] = (
                    f"exit={result.get('returncode', -1)}\n"
                    f"STDOUT:\n{result.get('stdout', '')}\n"
                    f"STDERR:\n{result.get('stderr', '')}"
                )

    _threading.Thread(target=_execute, daemon=True).start()
    return run_id


def get_vibe_run(run_id: str) -> dict:
    """Return the current status and output of a Vibe run."""
    with _VIBE_RUNS_LOCK:
        run = _VIBE_RUNS.get(run_id)
        if run is None:
            return {"status": "not_found", "output": "", "error": ""}
        return dict(run)


# ── Vibe iterative AI evaluation ──────────────────────────────────────────────

# Timeout (seconds) for a single vibe_evaluator call.
_VIBE_NEXT_TIMEOUT = 25


def handle_vibe_next(goal: str, history: list[dict], workspace: dict) -> dict:
    """Call the vibe_evaluator agent with the goal and conversation history.

    ``history`` is a list of ``{"command": str, "output": str}`` dicts,
    each entry representing one command that was already run and its SSH output.

    Returns a dict with either:
      ``{"done": True, "answer": "..."}``  — goal fully answered
    or
      ``{"done": False, "command": "...", "reason": "..."}``  — next command needed
    or an error dict when the agent fails.
    """
    _FALLBACK: dict = {
        "done": True,
        "answer": "Could not evaluate results — please review the command output manually.",
    }

    if budget.is_exhausted():
        return {
            "done": True,
            "answer": "Budget exhausted — AI evaluation skipped. Please review the output above.",
        }

    ctx_lines: list[str] = []
    if settings.ssh_host:
        ctx_lines.append(f"SSH target: {settings.ssh_host}")
    if settings.repo_dir:
        ctx_lines.append(f"Repo dir: {settings.repo_dir}")
    ctx_lines.append(_map_loader.get_summary())

    terminal_tail = (workspace.get("terminal_tail") or "").strip()
    if terminal_tail:
        ctx_lines.append("Last terminal output:\n" + "\n".join(terminal_tail.splitlines()[-200:]))

    ctx_lines.append(f"GOAL: {goal}")

    if history:
        history_parts: list[str] = []
        for i, step in enumerate(history, start=1):
            cmd = step.get("command", "(unknown)")
            out = (step.get("output") or "").strip()
            # Truncate individual outputs to keep prompt manageable.
            if len(out) > 3000:
                out = out[:3000] + "\n… (truncated)"
            history_parts.append(f"Step {i}:\n  command: {cmd}\n  output:\n{out}")
        ctx_lines.append("HISTORY:\n" + "\n\n".join(history_parts))
    else:
        ctx_lines.append("HISTORY: (none — no commands have been run yet)")

    prompt = "\n".join(ctx_lines)

    def _call():
        return Runner.run_sync(vibe_evaluator, prompt, max_turns=1)

    fut = EXECUTOR.submit(_call)
    try:
        result = fut.result(timeout=_VIBE_NEXT_TIMEOUT)
        _record_run_usage(result)
        parsed = _json.loads(result.final_output)
        # Normalise keys
        if parsed.get("done"):
            return {"done": True, "answer": parsed.get("answer", "(no answer provided)")}
        cmd = (parsed.get("command") or "").strip()
        if not cmd:
            return _FALLBACK
        return {
            "done": False,
            "command": cmd,
            "reason": parsed.get("reason", ""),
        }
    except _json.JSONDecodeError:
        return _FALLBACK
    except Exception:
        return _FALLBACK


# ── Autopilot investigate loop ────────────────────────────────────────────────

_AUTOPILOT_LOCK = _threading.Lock()
_AUTOPILOT_STOP = _threading.Event()

_AUTOPILOT_STATE: dict = {
    "running": False,
    "findings": [],   # list of finding dicts (max 100)
    "unread": 0,
    "last_run": None,  # ISO timestamp of last investigation
    "last_clear": None,  # ISO timestamp of last all-clear
    "interval": settings.autopilot_interval,
    "_next_run_mono": None,  # monotonic time of next scheduled run
    "events": [],   # live progress events (max 200)
}
_AUTOPILOT_FINDING_COUNTER = 0
_AUTOPILOT_EVENT_COUNTER = 0

# Recently-seen issue fingerprints — prevents opening duplicate GitHub issues.
# Capped at 200 entries; older entries are evicted when the cap is reached.
_AUTOPILOT_FINGERPRINTS: set[str] = set()
_AUTOPILOT_FINGERPRINT_ORDER: list[str] = []
_AUTOPILOT_FINGERPRINT_CAP = 200

# Timeout (seconds) for the vibe_planner conclude step inside each cycle.
_AUTOPILOT_CONCLUDE_TIMEOUT = 15


def _autopilot_push_event(kind: str, message: str) -> None:
    """Append a progress event to the autopilot events list. Thread-safe."""
    global _AUTOPILOT_EVENT_COUNTER
    with _AUTOPILOT_LOCK:
        _AUTOPILOT_EVENT_COUNTER += 1
        ev = {
            "id": _AUTOPILOT_EVENT_COUNTER,
            "t": _now_iso(),
            "kind": kind,
            "message": message,
        }
        _AUTOPILOT_STATE["events"].append(ev)
        _AUTOPILOT_STATE["events"] = _AUTOPILOT_STATE["events"][-200:]


def _ap_ssh(label: str, command: str, timeout: int = 12) -> str:
    """Run one evidence-pack SSH command, emit a progress event, return output section."""
    _autopilot_push_event("gather", f"📡 {label}…")
    result = run_ssh_readonly(command, timeout=timeout)
    if "error" in result:
        return f"=== {label} ===\n[SSH error: {result['error']}]\n"
    stdout = (result.get("stdout") or "").strip()
    stderr = (result.get("stderr") or "").strip()
    body = (stdout + ("\n" + stderr if stderr else "")).strip() or "(empty)"
    return f"=== {label} ===\n{body}\n"


def _autopilot_gather_evidence() -> str:
    """Collect a structured evidence pack via SSH. Returns multi-section string."""
    repo = settings.repo_dir or "/opt/openclaw-crypto"
    sections: list[str] = []

    # 1. Container inventory
    sections.append(_ap_ssh(
        "docker ps",
        "docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.State}}' 2>/dev/null || "
        "echo '[docker not available]'",
    ))

    # 2. Container inspect summaries (status / pid / startedAt for all containers)
    sections.append(_ap_ssh(
        "docker inspect",
        "docker ps -q 2>/dev/null | xargs -r docker inspect "
        "--format '{{.Name}} state={{.State.Status}} pid={{.State.Pid}} "
        "started={{.State.StartedAt}} restarts={{.RestartCount}}' 2>/dev/null || "
        "echo '[no containers or docker unavailable]'",
    ))

    # 3. Orchestrator logs (last 500 lines)
    sections.append(_ap_ssh(
        "docker logs (tail 500)",
        "(docker logs --tail=500 openclaw-orchestrator 2>&1) || "
        "echo '[no openclaw-orchestrator container]'",
        timeout=20,
    ))

    # 4. Keyword grep highlights from log output (capture directly from docker logs)
    sections.append(_ap_ssh(
        "log grep highlights",
        "(docker logs --tail=500 openclaw-orchestrator 2>&1 | "
        "grep -iE 'HALT|HALTED|pnl|sharpe|drawdown|risk|error|exception|connect|trade|signal' "
        "| tail -n 80) 2>/dev/null || echo '[no matches or container unavailable]'",
        timeout=20,
    ))

    # 5. Recent git history
    sections.append(_ap_ssh(
        "git log",
        f"cd {repo} && git log -n 50 --oneline 2>/dev/null || echo '[git unavailable]'",
    ))

    # 6. HALT grep across codebase
    sections.append(_ap_ssh(
        "code grep HALT",
        f"cd {repo} && grep -RIn 'HALT\\|HALTED\\|halt' . 2>/dev/null | head -n 200 || "
        "echo '[no matches]'",
        timeout=18,
    ))

    # 7. Risk/trading keyword grep
    sections.append(_ap_ssh(
        "code grep risk/trading",
        f"cd {repo} && grep -RIn 'drawdown\\|sharpe\\|risk\\|multiplier\\|bear' . "
        "2>/dev/null | head -n 200 || echo '[no matches]'",
        timeout=18,
    ))

    # 8. Config file discovery
    sections.append(_ap_ssh(
        "config files",
        f"find {repo} -maxdepth 3 \\( -name '*.env' -o -name '.env*' -o -name '*.env.*' "
        "-o -name '*.yml' -o -name '*.yaml' -o -name '*.toml' -o -name '*.json' \\) "
        "2>/dev/null | head -n 120 || echo '[find unavailable]'",
    ))

    return "\n".join(sections)


def _autopilot_issue_fingerprint(action_type: str, key: str) -> str:
    """Return a short hex fingerprint for deduplication."""
    raw = f"{action_type}:{key}".encode()
    return _hashlib.sha256(raw).hexdigest()[:16]


def _autopilot_is_duplicate(fingerprint: str) -> bool:
    """Return True if this fingerprint was seen recently; register it if not."""
    global _AUTOPILOT_FINGERPRINTS, _AUTOPILOT_FINGERPRINT_ORDER
    if fingerprint in _AUTOPILOT_FINGERPRINTS:
        return True
    # Register new fingerprint; evict oldest if cap exceeded
    _AUTOPILOT_FINGERPRINTS.add(fingerprint)
    _AUTOPILOT_FINGERPRINT_ORDER.append(fingerprint)
    if len(_AUTOPILOT_FINGERPRINT_ORDER) > _AUTOPILOT_FINGERPRINT_CAP:
        evict = _AUTOPILOT_FINGERPRINT_ORDER.pop(0)
        _AUTOPILOT_FINGERPRINTS.discard(evict)
    return False


def _build_autopilot_issue_body(
    issue_body: str,
    summary: str,
    recommended_action: str,
    evidence: str,
) -> str:
    """Combine the agent-generated body with the full evidence pack."""
    # Keep only the last 300 lines — enough context for Copilot without hitting
    # GitHub's 65 535-character issue body limit.
    evidence_lines = (evidence or "").splitlines()[-300:]
    evidence_snippet = "\n".join(evidence_lines) or "(no evidence collected)"

    lines = [
        "## Autopilot Finding",
        "",
        issue_body or summary,
        "",
        "## Recommended Action",
        recommended_action or "(see evidence below)",
        "",
        "## Context",
        f"- **Host (Vibe):** {settings.ssh_host or '(not configured)'}",
        f"- **Host (READONLY):** {settings.ssh_readonly_host or '(not configured)'}",
        f"- **Repo:** {settings.repo_dir or '(not configured)'}",
        "- **Triggered by:** OpenClaw Autopilot (automated investigation)",
        "",
        "## Constraints",
        "- No secrets or credentials added to source code",
        "- No destructive operations introduced",
        "- Changes limited to the minimum required by the issue",
        "- Match existing code style and conventions",
        "",
        "## Acceptance Criteria",
        "- [ ] Anomaly resolved as described above",
        "- [ ] Local test passed: `git pull; uvicorn web_app:app --reload;` then verified in browser",
        "",
        "## Evidence Pack (last 300 lines)",
        "```",
        evidence_snippet,
        "```",
    ]
    return "\n".join(lines)


def _autopilot_analyze(evidence: str) -> dict:
    """Feed the evidence pack to the investigate agent; return parsed JSON dict."""
    _FALLBACK: dict = {
        "needs_action": False,
        "urgency": "low",
        "summary": "Analysis unavailable.",
        "recommended_action": "",
        "action_type": "none",
        "target_repo": "",
        "issue_title": "",
        "issue_body": "",
        "vibe_command": "",
    }

    if budget.is_exhausted():
        return {
            **_FALLBACK,
            "summary": "Budget exhausted — AI analysis skipped.",
        }

    ctx_lines = []
    if settings.ssh_host:
        ctx_lines.append(f"SSH target (Vibe/execution): {settings.ssh_host}")
    if settings.ssh_readonly_host:
        ctx_lines.append(f"SSH target (READONLY/probes): {settings.ssh_readonly_host}")
    if settings.repo_dir:
        ctx_lines.append(f"Repo dir: {settings.repo_dir}")
    # Inject ops map so the investigate agent knows where to look and what paths to use.
    ctx_lines.append(_map_loader.get_summary())
    # Truncate evidence to ~12 000 chars — leaves headroom for system/context tokens
    # given a 16 k-token model context window (roughly 4 chars/token → ~3 000 tokens).
    ctx_lines.append(f"Evidence pack:\n{evidence[:12000]}")
    prompt = "\n".join(ctx_lines)

    def _call():
        return Runner.run_sync(investigate_agent, prompt, max_turns=1)

    fut = EXECUTOR.submit(_call)
    try:
        result = fut.result(timeout=30)
        _record_run_usage(result)
        parsed = _json.loads(result.final_output)
        # Back-fill missing keys with safe defaults
        for key, default in _FALLBACK.items():
            parsed.setdefault(key, default)
        if not parsed.get("needs_action"):
            parsed["action_type"] = "none"
        elif parsed["action_type"] not in ("github_issue", "vibe_action", "none"):
            parsed["action_type"] = "vibe_action"
        # Validate target_repo
        if parsed["action_type"] == "github_issue":
            tr = parsed.get("target_repo", "")
            if tr not in _GH_ALLOWED_REPOS:
                parsed["target_repo"] = settings.github_repo or "leeheggan-droid/openclaw-control"
        # ── Save autopilot evidence fingerprint to memory ──────────────────
        # Store a derived summary (not raw evidence) so subsequent agent calls
        # have context about the last investigation cycle.
        try:
            ct, gh = _vibe_reports.extract_fingerprint_fields(evidence)
            fp = _memory.compute_fingerprint(ct, gh)
            snap = {
                "last_autopilot_summary": parsed.get("summary", "")[:500],
                "last_autopilot_needs_action": str(parsed.get("needs_action", False)),
                "last_autopilot_urgency": parsed.get("urgency", "low"),
                "last_autopilot_at": datetime.now(_tz.utc).replace(microsecond=0).isoformat(),
            }
            for ag in ("pnl", "quant", "coo"):
                existing = _memory.load_snapshot(ag)
                existing.update(snap)
                _memory.save_snapshot(ag, existing, fp)
        except Exception:
            pass  # memory is best-effort; never block the autopilot cycle
        return parsed
    except Exception:
        return _FALLBACK


def _autopilot_conclude(summary: str, recommended_action: str) -> tuple[str, str]:
    """Use vibe_planner to generate workdir + prompt for the given finding.

    Returns a ``(workdir, prompt)`` tuple, or ``("", "")`` if generation fails.
    """
    ctx_lines = []
    if settings.ssh_host:
        ctx_lines.append(f"SSH target: {settings.ssh_host}")
    if settings.repo_dir:
        ctx_lines.append(f"Repo dir: {settings.repo_dir}")
    ctx_lines.append(
        f"System anomaly detected.\n"
        f"Summary: {summary}\n"
        f"Recommended action: {recommended_action}\n\n"
        "Output ONLY the JSON with workdir and prompt to resolve this issue on the VPS."
    )
    prompt = "\n".join(ctx_lines)

    def _call():
        return Runner.run_sync(vibe_planner, prompt, max_turns=1)

    fut = EXECUTOR.submit(_call)
    try:
        result = fut.result(timeout=_AUTOPILOT_CONCLUDE_TIMEOUT)
        _record_run_usage(result)
        parsed = _json.loads(result.final_output)
        return parsed.get("workdir", ""), parsed.get("prompt", "")
    except Exception as exc:
        _autopilot_push_event("error", f"⚠️ Command generation failed ({type(exc).__name__}) — operator must act manually.")
        return "", ""


def _autopilot_run_once() -> None:
    """Run a single investigation cycle: evidence → investigate → conclude → escalate."""
    global _AUTOPILOT_FINDING_COUNTER

    if not settings.ssh_readonly_host:
        _autopilot_push_event("skip", "READONLY SSH host not configured — skipping investigation.")
        with _AUTOPILOT_LOCK:
            _AUTOPILOT_STATE["last_run"] = _now_iso()
        return

    # Phase 1: Detect — build full evidence pack via SSH
    _autopilot_push_event("gather", "🔍 Building evidence pack via SSH…")
    evidence = _autopilot_gather_evidence()

    if not evidence.strip():
        _autopilot_push_event("error", "⚠️ No evidence returned from SSH.")
        with _AUTOPILOT_LOCK:
            interval = _AUTOPILOT_STATE["interval"]
            _AUTOPILOT_STATE["last_run"] = _now_iso()
            _AUTOPILOT_STATE["_next_run_mono"] = _time.monotonic() + interval
        return

    # Phase 2: Investigate — LLM analysis of the full evidence pack
    _autopilot_push_event("analyze", "🧠 Investigating evidence pack…")
    finding = _autopilot_analyze(evidence)

    if not finding.get("needs_action"):
        # All clear — resolve silently, update timestamps only
        _autopilot_push_event("clear", f"✅ All clear — {finding.get('summary', 'system healthy')}")
        with _AUTOPILOT_LOCK:
            interval = _AUTOPILOT_STATE["interval"]
            _AUTOPILOT_STATE["last_run"] = _now_iso()
            _AUTOPILOT_STATE["last_clear"] = _now_iso()
            _AUTOPILOT_STATE["_next_run_mono"] = _time.monotonic() + interval
        return

    summary = finding.get("summary", "")
    recommended = finding.get("recommended_action", "")
    urgency = finding.get("urgency", "low")
    action_type = finding.get("action_type", "vibe_action")

    _autopilot_push_event("conclude", f"⚠️ Issue detected ({urgency.upper()}): {summary}")

    # Phase 3: Conclude — branch by action_type
    # Legacy field support: agent finding may still provide a raw vibe_command string;
    # we prefer vibe_workdir/vibe_prompt if present.
    vibe_workdir = finding.get("vibe_workdir", "")
    vibe_prompt_text = finding.get("vibe_prompt", "")
    if not vibe_workdir and not vibe_prompt_text:
        # Fall back to parsing a legacy vibe_command string if provided by the agent
        legacy_cmd = finding.get("vibe_command", "")
        if legacy_cmd:
            # Treat the whole legacy command as the prompt with the default workdir
            vibe_workdir = settings.vibe_workdir or settings.repo_dir or "/opt/openclaw-crypto"
            vibe_prompt_text = legacy_cmd
    github_issue_url = ""
    github_issue_number = None

    if action_type == "github_issue":
        issue_title = (finding.get("issue_title") or f"[Autopilot] {summary[:72]}").strip()
        issue_body_agent = finding.get("issue_body", "")
        target_repo = finding.get("target_repo") or settings.github_repo or "leeheggan-droid/openclaw-control"

        # Deduplication: skip if we already opened an issue for the same title/repo
        fingerprint = _autopilot_issue_fingerprint("github_issue", f"{target_repo}:{issue_title[:60]}")
        if _autopilot_is_duplicate(fingerprint):
            _autopilot_push_event(
                "escalate",
                f"🔁 Duplicate suppressed — issue already opened for: {issue_title[:60]}",
            )
            action_type = "none"  # downgrade to plain finding (no new issue)
        else:
            _autopilot_push_event("escalate", f"📂 Opening GitHub issue in {target_repo}…")
            full_body = _build_autopilot_issue_body(issue_body_agent, summary, recommended, evidence)
            result = create_github_issue(
                title=issue_title,
                body=full_body,
                repo_full=target_repo,
                labels=["autopilot", "bug"],
                assign_copilot=True,
            )
            if result:
                github_issue_url = result["issue_url"]
                github_issue_number = result["issue_number"]
                _autopilot_push_event(
                    "escalate",
                    f"✅ GitHub issue #{github_issue_number} created: {github_issue_url}",
                )
            else:
                _autopilot_push_event(
                    "escalate",
                    "⚠️ Could not create GitHub issue (GITHUB_TOKEN missing or API error) — operator must act manually.",
                )

    elif action_type == "vibe_action":
        # State change needed — use agent-generated vibe fields if present, else call planner
        if not vibe_workdir and not vibe_prompt_text:
            _autopilot_push_event("conclude", "💡 Generating remediation command…")
            vibe_workdir, vibe_prompt_text = _autopilot_conclude(summary, recommended)
        if vibe_workdir or vibe_prompt_text:
            _autopilot_push_event("escalate", f"📋 Proposed vibe: workdir={vibe_workdir!r} prompt={vibe_prompt_text!r}")
            _autopilot_push_event("escalate", "⏳ Awaiting operator approval via Vibe.")
        else:
            action_text = recommended or "(see finding)"
            _autopilot_push_event("escalate", f"📋 Recommended action: {action_text}")
            _autopilot_push_event("escalate", "⏳ Operator review required.")

    else:
        # Plain finding — surface for manual review
        action_text = recommended or "(see finding)"
        _autopilot_push_event("escalate", f"📋 Recommended action: {action_text}")
        _autopilot_push_event("escalate", "⏳ Operator review required.")

    # Phase 4: Escalate — record finding for operator review
    with _AUTOPILOT_LOCK:
        _AUTOPILOT_FINDING_COUNTER += 1
        interval = _AUTOPILOT_STATE["interval"]
        entry = {
            "id": _AUTOPILOT_FINDING_COUNTER,
            "t": _now_iso(),
            "urgency": urgency,
            "summary": summary,
            "recommended_action": recommended,
            "action_type": action_type,
            "vibe_workdir": vibe_workdir,
            "vibe_prompt": vibe_prompt_text,
            "github_issue_url": github_issue_url,
            "github_issue_number": github_issue_number,
            "acked": False,
        }
        _AUTOPILOT_STATE["findings"].append(entry)
        # Keep only the most recent 100 findings
        _AUTOPILOT_STATE["findings"] = _AUTOPILOT_STATE["findings"][-100:]
        _AUTOPILOT_STATE["unread"] += 1
        _AUTOPILOT_STATE["last_run"] = _now_iso()
        _AUTOPILOT_STATE["_next_run_mono"] = _time.monotonic() + interval

def _autopilot_loop(stop_event: _threading.Event) -> None:
    """Background daemon: run investigations on a fixed interval."""
    while not stop_event.is_set():
        _autopilot_run_once()
        with _AUTOPILOT_LOCK:
            interval = _AUTOPILOT_STATE["interval"]
        stop_event.wait(timeout=interval)
    with _AUTOPILOT_LOCK:
        _AUTOPILOT_STATE["running"] = False
        _AUTOPILOT_STATE["_next_run_mono"] = None


def start_autopilot(interval: int | None = None) -> dict:
    """Start the autopilot background loop. Returns status dict."""
    global _AUTOPILOT_STOP
    with _AUTOPILOT_LOCK:
        if _AUTOPILOT_STATE["running"]:
            return {"status": "already_running"}
        effective = max(30, interval or settings.autopilot_interval)  # 30 s floor prevents accidental tight loops
        _AUTOPILOT_STATE["interval"] = effective
        _AUTOPILOT_STATE["running"] = True
        _AUTOPILOT_STOP = _threading.Event()

    _threading.Thread(
        target=_autopilot_loop,
        args=(_AUTOPILOT_STOP,),
        daemon=True,
    ).start()
    return {"status": "started", "interval": effective}


def stop_autopilot() -> dict:
    """Stop the autopilot background loop."""
    with _AUTOPILOT_LOCK:
        if not _AUTOPILOT_STATE["running"]:
            return {"status": "not_running"}
    _AUTOPILOT_STOP.set()
    return {"status": "stopping"}


def get_autopilot_status() -> dict:
    """Return current autopilot state for the UI status bar."""
    with _AUTOPILOT_LOCK:
        next_mono = _AUTOPILOT_STATE.get("_next_run_mono")
        secs = int(max(0, next_mono - _time.monotonic())) if next_mono else None
        return {
            "running": _AUTOPILOT_STATE["running"],
            "interval": _AUTOPILOT_STATE["interval"],
            "last_run": _AUTOPILOT_STATE["last_run"],
            "last_clear": _AUTOPILOT_STATE["last_clear"],
            "unread": _AUTOPILOT_STATE["unread"],
            "finding_count": len(_AUTOPILOT_STATE["findings"]),
            "event_count": _AUTOPILOT_EVENT_COUNTER,
            "seconds_until_next_run": secs,
        }


def get_autopilot_findings(cursor: int = 0) -> dict:
    """Return findings from the given cursor position."""
    with _AUTOPILOT_LOCK:
        findings = list(_AUTOPILOT_STATE["findings"][cursor:])
    return {"findings": findings}


def get_autopilot_events(cursor: int = 0) -> dict:
    """Return live progress events from the given cursor position."""
    with _AUTOPILOT_LOCK:
        events = list(_AUTOPILOT_STATE["events"][cursor:])
        total = _AUTOPILOT_EVENT_COUNTER
    return {"events": events, "total": total}


def ack_autopilot_findings() -> dict:
    """Mark all findings as acknowledged (clears the unread badge)."""
    with _AUTOPILOT_LOCK:
        _AUTOPILOT_STATE["unread"] = 0
        for f in _AUTOPILOT_STATE["findings"]:
            f["acked"] = True
    return {"status": "ok"}
