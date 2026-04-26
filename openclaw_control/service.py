import atexit as _atexit
import hashlib as _hashlib
import json as _json
import re as _re
import subprocess
import threading as _threading
import time as _time
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone as _tz

from agents import Agent, ModelSettings, Runner, RunResult, SQLiteSession, SessionSettings
from openclaw_control import budget
from openclaw_control.budget import COO_BUDGET_MESSAGE
from openclaw_control.config import settings
from openclaw_control.github_tools import ALLOWED_REPOS as _GH_ALLOWED_REPOS, create_github_issue
from openclaw_control.agents.controller import controller
from openclaw_control.agents.analysis_agent import analysis_agent
from openclaw_control.agents.router import route_message
from openclaw_control.agents.main_agent import main_agent
from openclaw_control.agents.pnl_agent import pnl_agent
from openclaw_control.agents.quant_agent import quant_agent
from openclaw_control.agents.coo_agent import coo_agent
from openclaw_control.agents.vibe_agent import vibe_planner
from openclaw_control.agents.investigate_agent import investigate_agent
from openclaw_control.ops import map_loader as _map_loader
from openclaw_control.memory import agent_memory as _memory
from openclaw_control import vibe_reports as _vibe_reports

EXECUTOR = ThreadPoolExecutor(max_workers=2)

_SESSION_SETTINGS = SessionSettings(limit=100)

_ops_session = SQLiteSession("openclaw_ops_session", session_settings=_SESSION_SETTINGS)
_analysis_session = SQLiteSession("openclaw_analysis_session", session_settings=_SESSION_SETTINGS)
_main_session = SQLiteSession("openclaw_main_session", session_settings=_SESSION_SETTINGS)
_pnl_session = SQLiteSession("openclaw_pnl_session", session_settings=_SESSION_SETTINGS)
_quant_session = SQLiteSession("openclaw_quant_session", session_settings=_SESSION_SETTINGS)
_coo_session = SQLiteSession("openclaw_coo_session", session_settings=_SESSION_SETTINGS)
_vibe_session = SQLiteSession("openclaw_vibe_session", session_settings=_SESSION_SETTINGS)

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


def run_vibe_report(report_id: str) -> str:
    """Execute all read-only SSH commands for *report_id* and return combined output.

    Each command's output is separated by a section header.  Returns a message
    string (not empty) when SSH is not configured or all commands produce no output.
    """
    if not settings.ssh_host:
        return "[SSH not configured — cannot run Vibe report]"
    commands = _report_commands_from_map().get(report_id.lower())
    if not commands:
        valid = ", ".join(_report_commands_from_map().keys())
        return f"[Unknown report_id '{report_id}'. Valid ids: {valid}]"
    sections: list[str] = []
    for cmd in commands:
        result = run_ssh(cmd, timeout=15)
        if "error" in result:
            sections.append(f"--- {cmd[:60]}... ---\n[SSH error]")
            continue
        out = (result.get("stdout") or "").strip()
        err = (result.get("stderr") or "").strip()
        body = (out + ("\n" + err if err else "")).strip() or "(empty)"
        sections.append(f"--- {cmd[:60]}... ---\n{body}")
    return "\n\n".join(sections)



def run_ssh(command: str, timeout: int = 10) -> dict:
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             settings.ssh_host, command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "type": "ssh",
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except Exception as e:
        return {
            "type": "ssh",
            "error": f"SSH error ({type(e).__name__}). Check server logs.",
        }


def run_agent(text: str) -> dict:
    route = route_message(text)

    agent = controller if route.name == "ops" else analysis_agent
    session = _ops_session if route.name == "ops" else _analysis_session
    label = route.name

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
# Main is unbounded (uses Runner default).
# Specialists (no tools) need only 1 turn; COO needs up to 3 (tool × 2 + final memo).
_MAX_TURNS: dict[str, int | None] = {
    "main":  None,   # unbounded
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
_AGENT_TIMEOUT: dict[str, int] = {
    "coo": 15,
}
_DEFAULT_TIMEOUT = 25


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
            if not stored_fp.startswith(current_fp_config_only[:12]):
                _memory.invalidate(name, reason="SSH target or repo_dir changed")
                snap = {}
        if snap:
            # Summarise the snapshot for prompt injection; never inject raw blobs.
            mem_lines = ["\n=== AGENT MEMORY (evidence-based, auto-refreshed) ==="]
            for k, v in snap.items():
                if isinstance(v, str) and len(v) < 500:
                    mem_lines.append(f"  [{k}] {v}")
                elif isinstance(v, (int, float, bool)):
                    mem_lines.append(f"  [{k}] {v}")
            mem_lines.append("=== END AGENT MEMORY ===")
            ctx_lines.append("\n".join(mem_lines))

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
        # ------------------------------------------------------------------
        if name in ("main", "pnl", "quant") and settings.ssh_host:
            match = _VIBE_REQUEST_RE.search(final_output)
            if match:
                report_id = match.group(1).lower()
                # Validate against the known set before executing any SSH commands.
                _known_ids = set(_report_commands_from_map().keys())
                if report_id in _known_ids:
                    report_data = run_vibe_report(report_id)
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
                        if name in ("pnl", "quant") and len(report_data) > 50 and "(empty)" not in report_data and "[SSH error]" not in report_data:
                            ct, gh = _vibe_reports.extract_fingerprint_fields(report_data)
                            fp = _memory.compute_fingerprint(ct, gh)
                            existing_snap = _memory.load_snapshot(name)
                            existing_snap[f"last_{report_id}"] = report_data[:1000]
                            existing_snap[f"last_{report_id}_at"] = datetime.now(_tz.utc).replace(microsecond=0).isoformat()
                            existing_snap["last_report_id"] = report_id
                            _memory.save_snapshot(name, existing_snap, fp)
                    except (FuturesTimeout, Exception):
                        pass  # fall back to the original output that had the request

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


def _now_iso() -> str:
    return datetime.now(_tz.utc).replace(microsecond=0).isoformat()


def _push_event(run: dict, agent: str, etype: str, content: str) -> None:
    ev = {"t": _now_iso(), "agent": agent, "type": etype, "content": content}
    with run["lock"]:
        run["events"].append(ev)


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


def _gather_vibe_snapshot(run: dict, timeout_s: float) -> str:
    """Probe the server with read-only SSH commands, emitting one feed event per probe.

    Each command streams its label and result snippet into the 'vibe' feed as it
    completes — giving the same step-by-step building feel as the Autopilot tab.
    Returns the full aggregated snapshot string for injection into agent prompts.
    """
    if not settings.ssh_host:
        _push_event(run, "vibe", "message", "SSH not configured — snapshot skipped")
        return ""

    _push_event(run, "vibe", "start", "")
    sections: list[str] = []
    t_deadline = _time.monotonic() + timeout_s
    repo = settings.repo_dir or "/opt/openclaw-crypto"

    PROBES = [
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
    ]

    for label, cmd, t in PROBES:
        if _time.monotonic() >= t_deadline:
            _push_event(run, "vibe", "message", "⏱ Time budget low — remaining probes skipped")
            break
        # Emit the "asking…" line so the feed shows intent before the SSH round-trip
        _push_event(run, "vibe", "message", f"📡 {label}…")
        r = run_ssh(cmd, timeout=t)
        if "error" in r:
            _push_event(run, "vibe", "message", f"  ↳ [SSH error]")
            continue
        out = (r.get("stdout") or "").strip()
        if out:
            snippet = "\n".join(out.splitlines()[:12])
            _push_event(run, "vibe", "message", f"  ↳ {snippet}")
            sections.append(f"=== {label} ===\n{out}")
        else:
            _push_event(run, "vibe", "message", f"  ↳ (no output)")

    _push_event(run, "vibe", "done", "")
    return "\n\n".join(sections).strip()


def _run_for_team(agent, prompt: str, timeout_s: float) -> tuple[str, bool]:
    """Submit agent to the team executor (single pass, no session). Returns (output, timed_out)."""
    fut = _TEAM_EXECUTOR.submit(Runner.run_sync, agent, prompt, max_turns=1)
    try:
        res = fut.result(timeout=max(1.0, timeout_s))
        _record_run_usage(res)
        return res.final_output, False
    except FuturesTimeout:
        # cancel() is a no-op on already-running futures; the thread will finish naturally.
        return f"[{agent.name} timed out]", True
    except Exception as exc:
        return f"[{agent.name} error: {type(exc).__name__}]", False


def _make_agent_prompt(user_prompt: str, ctx: str, bprefix: str = "") -> str:
    body = f"{ctx}\n\n{user_prompt}" if ctx else user_prompt
    return (bprefix + body) if bprefix else body


def _orchestrate_quick(run: dict, user_prompt: str, ctx: str, deadline: float) -> None:
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

    # Explore the live server — each probe streams into the feed as it completes
    rem = deadline - _time.monotonic()
    vibe_snap = ""
    if rem > 12:
        vibe_snap = _gather_vibe_snapshot(run, min(10.0, rem - 10))
    else:
        _push_event(run, "vibe", "message", "⏱ Skipped — time budget low")

    # Evidence gate: warn if SSH is configured but Vibe returned no data.
    if not vibe_snap and settings.ssh_host:
        _push_event(
            run, "vibe", "message",
            "⚠️ No live server evidence returned — analysis may be incomplete. "
            "Check SSH connectivity and container name (expected: openclaw-orchestrator).",
        )

    enhanced_ctx = (ctx + "\n\n=== Live Server Snapshot ===\n" + vibe_snap) if vibe_snap else ctx

    bprefix = _BUDGET_LOW_PREFIX if budget.is_low() else ""
    pnl_prompt = _make_agent_prompt(user_prompt, enhanced_ctx, bprefix)
    quant_prompt = _make_agent_prompt(user_prompt, enhanced_ctx, bprefix)

    # Submit P&L and Quant in parallel
    _push_event(run, "pnl", "start", "")
    _push_event(run, "quant", "start", "")
    pnl_fut = _TEAM_EXECUTOR.submit(Runner.run_sync, pnl_agent, pnl_prompt, max_turns=1)
    quant_fut = _TEAM_EXECUTOR.submit(Runner.run_sync, quant_agent, quant_prompt, max_turns=1)

    pnl_out = ""
    try:
        r = pnl_fut.result(timeout=max(1.0, deadline - _time.monotonic()))
        _record_run_usage(r)
        pnl_out = r.final_output
        _push_event(run, "pnl", "message", pnl_out)
        _push_event(run, "pnl", "done", "")
    except FuturesTimeout:
        pnl_out = "[P&L timed out]"
        _push_event(run, "pnl", "error", pnl_out)
    except Exception as exc:
        pnl_out = f"[P&L error: {type(exc).__name__}]"
        _push_event(run, "pnl", "error", pnl_out)

    quant_out = ""
    try:
        r = quant_fut.result(timeout=max(1.0, deadline - _time.monotonic()))
        _record_run_usage(r)
        quant_out = r.final_output
        _push_event(run, "quant", "message", quant_out)
        _push_event(run, "quant", "done", "")
    except FuturesTimeout:
        quant_out = "[Quant timed out]"
        _push_event(run, "quant", "error", quant_out)
    except Exception as exc:
        quant_out = f"[Quant error: {type(exc).__name__}]"
        _push_event(run, "quant", "error", quant_out)

    # COO synthesis — uses remaining time budget
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


def _orchestrate_detailed(run: dict, user_prompt: str, ctx: str, deadline: float) -> None:
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

    # Explore the live server — each probe streams into the feed as it completes
    rem = deadline - _time.monotonic()
    vibe_snap = ""
    if rem > 18:
        vibe_snap = _gather_vibe_snapshot(run, min(14.0, rem - 14))
    else:
        _push_event(run, "vibe", "message", "⏱ Skipped — time budget low")

    # Evidence gate: warn if SSH is configured but Vibe returned no data.
    if not vibe_snap and settings.ssh_host:
        _push_event(
            run, "vibe", "message",
            "⚠️ No live server evidence returned — analysis may be incomplete. "
            "Check SSH connectivity and container name (expected: openclaw-orchestrator).",
        )

    enhanced_ctx = (ctx + "\n\n=== Live Server Snapshot ===\n" + vibe_snap) if vibe_snap else ctx

    bprefix = _BUDGET_LOW_PREFIX if budget.is_low() else ""

    # ── Round 1 ───────────────────────────────────────────────────────────────
    _push_event(run, "system", "message", "── Round 1 ──")

    pnl_prompt = _make_agent_prompt(user_prompt, enhanced_ctx, bprefix)
    quant_prompt = _make_agent_prompt(user_prompt, enhanced_ctx, bprefix)

    _push_event(run, "pnl", "start", "Round 1")
    _push_event(run, "quant", "start", "Round 1")
    pnl_fut = _TEAM_EXECUTOR.submit(Runner.run_sync, pnl_agent, pnl_prompt, max_turns=1)
    quant_fut = _TEAM_EXECUTOR.submit(Runner.run_sync, quant_agent, quant_prompt, max_turns=1)

    pnl_r1 = ""
    try:
        r = pnl_fut.result(timeout=max(1.0, deadline - _time.monotonic()))
        _record_run_usage(r)
        pnl_r1 = r.final_output
        _push_event(run, "pnl", "message", pnl_r1)
        _push_event(run, "pnl", "done", "Round 1")
    except FuturesTimeout:
        pnl_r1 = "[P&L R1 timed out]"
        _push_event(run, "pnl", "error", pnl_r1)
    except Exception as exc:
        pnl_r1 = f"[P&L R1 error: {type(exc).__name__}]"
        _push_event(run, "pnl", "error", pnl_r1)

    quant_r1 = ""
    try:
        r = quant_fut.result(timeout=max(1.0, deadline - _time.monotonic()))
        _record_run_usage(r)
        quant_r1 = r.final_output
        _push_event(run, "quant", "message", quant_r1)
        _push_event(run, "quant", "done", "Round 1")
    except FuturesTimeout:
        quant_r1 = "[Quant R1 timed out]"
        _push_event(run, "quant", "error", quant_r1)
    except Exception as exc:
        quant_r1 = f"[Quant R1 error: {type(exc).__name__}]"
        _push_event(run, "quant", "error", quant_r1)

    rem = deadline - _time.monotonic()
    if rem < 3:
        _push_event(run, "coo", "error", "[COO R1 timed out — time budget exhausted]")
        return

    coo_r1_prompt = (
        f"{enhanced_ctx}\n\n=== P&L Report (Round 1) ===\n{pnl_r1}\n\n"
        f"=== Quant Report (Round 1) ===\n{quant_r1}\n\n"
        f"=== Directive ===\n{user_prompt}"
    )
    _push_event(run, "coo", "start", "Round 1")
    coo_r1, r1_timeout = _run_for_team(_team_coo, coo_r1_prompt, timeout_s=rem)
    _push_event(run, "coo", "message", coo_r1)
    _push_event(run, "coo", "done", "Round 1")

    # ── Round 2 (if time permits) ─────────────────────────────────────────────
    rem = deadline - _time.monotonic()
    if rem < 6 or r1_timeout:
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

    _push_event(run, "pnl", "start", "Round 2")
    _push_event(run, "quant", "start", "Round 2")
    pnl_r2_fut = _TEAM_EXECUTOR.submit(Runner.run_sync, pnl_agent, pnl_r2_prompt, max_turns=1)
    quant_r2_fut = _TEAM_EXECUTOR.submit(Runner.run_sync, quant_agent, quant_r2_prompt, max_turns=1)

    pnl_r2 = ""
    try:
        r = pnl_r2_fut.result(timeout=max(1.0, deadline - _time.monotonic()))
        _record_run_usage(r)
        pnl_r2 = r.final_output
        _push_event(run, "pnl", "message", pnl_r2)
        _push_event(run, "pnl", "done", "Round 2")
    except FuturesTimeout:
        pnl_r2 = "[P&L R2 timed out]"
        _push_event(run, "pnl", "error", pnl_r2)
    except Exception as exc:
        pnl_r2 = f"[P&L R2 error: {type(exc).__name__}]"
        _push_event(run, "pnl", "error", pnl_r2)

    quant_r2 = ""
    try:
        r = quant_r2_fut.result(timeout=max(1.0, deadline - _time.monotonic()))
        _record_run_usage(r)
        quant_r2 = r.final_output
        _push_event(run, "quant", "message", quant_r2)
        _push_event(run, "quant", "done", "Round 2")
    except FuturesTimeout:
        quant_r2 = "[Quant R2 timed out]"
        _push_event(run, "quant", "error", quant_r2)
    except Exception as exc:
        quant_r2 = f"[Quant R2 error: {type(exc).__name__}]"
        _push_event(run, "quant", "error", quant_r2)

    rem = deadline - _time.monotonic()
    if rem < 3:
        _push_event(run, "coo", "error", "[COO final timed out — time budget exhausted]")
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


def start_team_review(mode: str, prompt: str, workspace: dict) -> str:
    """Start a team review orchestration run in a background thread. Returns run_id."""
    run_id = _uuid.uuid4().hex[:8]
    run: dict = {"events": [], "done": False, "lock": _threading.Lock()}
    with _TEAM_RUNS_LOCK:
        _TEAM_RUNS[run_id] = run

    timeout = 35 if mode == "quick" else 65

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


def start_vibe_run(command: str) -> str:
    """Run an approved shell command on the VPS via SSH in a background thread. Returns run_id."""
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
        result = run_ssh(command)
        with _VIBE_RUNS_LOCK:
            if "error" in result:
                run["status"] = "error"
                run["error"] = result["error"]
            else:
                run["status"] = "done"
                run["output"] = (
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
    result = run_ssh(command, timeout=timeout)
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
        f"- **Host:** {settings.ssh_host or '(not configured)'}",
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
        ctx_lines.append(f"SSH target: {settings.ssh_host}")
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


def _autopilot_conclude(summary: str, recommended_action: str) -> str:
    """Use vibe_planner to generate a concrete shell command for the given finding.

    Returns the command string, or empty string if generation fails.
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
        "Output ONLY the JSON command to resolve this issue on the VPS."
    )
    prompt = "\n".join(ctx_lines)

    def _call():
        return Runner.run_sync(vibe_planner, prompt, max_turns=1)

    fut = EXECUTOR.submit(_call)
    try:
        result = fut.result(timeout=_AUTOPILOT_CONCLUDE_TIMEOUT)
        _record_run_usage(result)
        parsed = _json.loads(result.final_output)
        return parsed.get("command", "")
    except Exception as exc:
        _autopilot_push_event("error", f"⚠️ Command generation failed ({type(exc).__name__}) — operator must act manually.")
        return ""


def _autopilot_run_once() -> None:
    """Run a single investigation cycle: evidence → investigate → conclude → escalate."""
    global _AUTOPILOT_FINDING_COUNTER

    if not settings.ssh_host:
        _autopilot_push_event("skip", "SSH host not configured — skipping investigation.")
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
    vibe_command = finding.get("vibe_command", "")
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
        # State change needed — use agent-generated vibe_command if present, else call planner
        if not vibe_command:
            _autopilot_push_event("conclude", "💡 Generating remediation command…")
            vibe_command = _autopilot_conclude(summary, recommended)
        if vibe_command:
            _autopilot_push_event("escalate", f"📋 Proposed command: {vibe_command}")
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
            "vibe_command": vibe_command,
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
