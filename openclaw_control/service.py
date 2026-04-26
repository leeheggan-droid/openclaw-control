import atexit as _atexit
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
from openclaw_control.agents.controller import controller
from openclaw_control.agents.analysis_agent import analysis_agent
from openclaw_control.agents.router import route_message
from openclaw_control.agents.main_agent import main_agent
from openclaw_control.agents.pnl_agent import pnl_agent
from openclaw_control.agents.quant_agent import quant_agent
from openclaw_control.agents.coo_agent import coo_agent
from openclaw_control.agents.vibe_agent import vibe_planner
from openclaw_control.agents.investigate_agent import investigate_agent

EXECUTOR = ThreadPoolExecutor(max_workers=2)

_SESSION_SETTINGS = SessionSettings(limit=100)

_ops_session = SQLiteSession("openclaw_ops_session", session_settings=_SESSION_SETTINGS)
_analysis_session = SQLiteSession("openclaw_analysis_session", session_settings=_SESSION_SETTINGS)
_main_session = SQLiteSession("openclaw_main_session", session_settings=_SESSION_SETTINGS)
_pnl_session = SQLiteSession("openclaw_pnl_session", session_settings=_SESSION_SETTINGS)
_quant_session = SQLiteSession("openclaw_quant_session", session_settings=_SESSION_SETTINGS)
_coo_session = SQLiteSession("openclaw_coo_session", session_settings=_SESSION_SETTINGS)
_vibe_session = SQLiteSession("openclaw_vibe_session", session_settings=_SESSION_SETTINGS)


def run_ssh(command: str) -> dict:
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             settings.ssh_host, command],
            capture_output=True,
            text=True,
            timeout=10,
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

    def _call():
        return Runner.run_sync(agent, prompt, **kwargs)

    future = EXECUTOR.submit(_call)
    try:
        result = future.result(timeout=timeout_s)
        _record_run_usage(result)
        return {"agent": name, "output": result.final_output}
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
    tail = (workspace.get("terminal_tail") or "").strip()
    if tail:
        capped = "\n".join(tail.splitlines()[-200:])
        lines.append(f"Last terminal output:\n{capped}")
    return "\n".join(lines)


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
    bprefix = _BUDGET_LOW_PREFIX if budget.is_low() else ""
    pnl_prompt = _make_agent_prompt(user_prompt, ctx, bprefix)
    quant_prompt = _make_agent_prompt(user_prompt, ctx, bprefix)

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
        f"{ctx}\n\n=== P&L Report ===\n{pnl_out}\n\n"
        f"=== Quant Report ===\n{quant_out}\n\n"
        f"=== Directive ===\n{user_prompt}"
    )
    _push_event(run, "coo", "start", "")
    coo_out, _ = _run_for_team(_team_coo, coo_prompt, timeout_s=rem)
    _push_event(run, "coo", "message", coo_out)
    _push_event(run, "coo", "done", "")


def _orchestrate_detailed(run: dict, user_prompt: str, ctx: str, deadline: float) -> None:
    bprefix = _BUDGET_LOW_PREFIX if budget.is_low() else ""

    # ── Round 1 ───────────────────────────────────────────────────────────────
    _push_event(run, "system", "message", "── Round 1 ──")

    pnl_prompt = _make_agent_prompt(user_prompt, ctx, bprefix)
    quant_prompt = _make_agent_prompt(user_prompt, ctx, bprefix)

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
        f"{ctx}\n\n=== P&L Report (Round 1) ===\n{pnl_r1}\n\n"
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
    pnl_r2_prompt = _make_agent_prompt(revise_directive, ctx, bprefix)
    quant_r2_prompt = _make_agent_prompt(revise_directive, ctx, bprefix)

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
        f"{ctx}\n\n=== P&L (Round 1) ===\n{pnl_r1}\n\n"
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

    timeout = 25 if mode == "quick" else 45
    user_prompt = (prompt or "").strip() or DEFAULT_TEAM_PROMPT
    ctx = _build_team_ctx(workspace)

    _push_event(run, "system", "run-start", f"mode={mode}")

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
}
_AUTOPILOT_FINDING_COUNTER = 0

# SSH commands run on every autopilot investigation cycle.
_INVESTIGATE_CMD = (
    "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.State}}' 2>/dev/null; "
    "echo '---LOGS---'; "
    "(docker logs --tail=100 openclaw-orchestrator 2>&1 || "
    "echo '[no openclaw-orchestrator container]')"
)


def _autopilot_gather_ssh() -> str:
    """Run the investigation SSH commands and return concatenated output."""
    result = run_ssh(_INVESTIGATE_CMD)
    if "error" in result:
        return f"[SSH error: {result['error']}]"
    stdout = (result.get("stdout") or "").strip()
    stderr = (result.get("stderr") or "").strip()
    return (stdout + ("\n" + stderr if stderr else "")).strip()


def _autopilot_analyze(raw: str) -> dict:
    """Feed gathered output to the investigate agent; return parsed JSON dict."""
    import json as _json

    if budget.is_exhausted():
        return {
            "needs_action": False,
            "urgency": "low",
            "summary": "Budget exhausted — AI analysis skipped.",
            "recommended_action": "",
        }

    ctx_lines = []
    if settings.ssh_host:
        ctx_lines.append(f"SSH target: {settings.ssh_host}")
    if settings.repo_dir:
        ctx_lines.append(f"Repo dir: {settings.repo_dir}")
    ctx_lines.append(f"System check output:\n{raw}")
    prompt = "\n".join(ctx_lines)

    def _call():
        return Runner.run_sync(investigate_agent, prompt, max_turns=1)

    fut = EXECUTOR.submit(_call)
    try:
        result = fut.result(timeout=20)
        _record_run_usage(result)
        return _json.loads(result.final_output)
    except Exception:
        return {
            "needs_action": False,
            "urgency": "low",
            "summary": "Analysis unavailable.",
            "recommended_action": "",
        }


def _autopilot_run_once() -> None:
    """Run a single investigation cycle (gather + analyze + record)."""
    global _AUTOPILOT_FINDING_COUNTER

    if not settings.ssh_host:
        with _AUTOPILOT_LOCK:
            _AUTOPILOT_STATE["last_run"] = _now_iso()
        return

    raw = _autopilot_gather_ssh()
    finding = _autopilot_analyze(raw) if raw else {
        "needs_action": False,
        "urgency": "low",
        "summary": "No system data available.",
        "recommended_action": "",
    }

    with _AUTOPILOT_LOCK:
        interval = _AUTOPILOT_STATE["interval"]
        _AUTOPILOT_STATE["last_run"] = _now_iso()
        _AUTOPILOT_STATE["_next_run_mono"] = _time.monotonic() + interval

        if finding.get("needs_action"):
            _AUTOPILOT_FINDING_COUNTER += 1
            entry = {
                "id": _AUTOPILOT_FINDING_COUNTER,
                "t": _now_iso(),
                "urgency": finding.get("urgency", "low"),
                "summary": finding.get("summary", ""),
                "recommended_action": finding.get("recommended_action", ""),
                "acked": False,
            }
            _AUTOPILOT_STATE["findings"].append(entry)
            # Keep only the most recent 100 findings
            _AUTOPILOT_STATE["findings"] = _AUTOPILOT_STATE["findings"][-100:]
            _AUTOPILOT_STATE["unread"] += 1
        else:
            _AUTOPILOT_STATE["last_clear"] = _now_iso()


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
            "seconds_until_next_run": secs,
        }


def get_autopilot_findings(cursor: int = 0) -> dict:
    """Return findings from the given cursor position."""
    with _AUTOPILOT_LOCK:
        findings = list(_AUTOPILOT_STATE["findings"][cursor:])
    return {"findings": findings}


def ack_autopilot_findings() -> dict:
    """Mark all findings as acknowledged (clears the unread badge)."""
    with _AUTOPILOT_LOCK:
        _AUTOPILOT_STATE["unread"] = 0
        for f in _AUTOPILOT_STATE["findings"]:
            f["acked"] = True
    return {"status": "ok"}
