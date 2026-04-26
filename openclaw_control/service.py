import atexit as _atexit
import subprocess
import threading as _threading
import time as _time
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone as _tz

from agents import Agent, ModelSettings, Runner, RunResult, SQLiteSession
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

EXECUTOR = ThreadPoolExecutor(max_workers=2)

_ops_session = SQLiteSession("openclaw_ops_session")
_analysis_session = SQLiteSession("openclaw_analysis_session")
_main_session = SQLiteSession("openclaw_main_session")
_pnl_session = SQLiteSession("openclaw_pnl_session")
_quant_session = SQLiteSession("openclaw_quant_session")
_coo_session = SQLiteSession("openclaw_coo_session")
_vibe_session = SQLiteSession("openclaw_vibe_session")


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

    # Build workspace context header (server-authoritative values + client terminal tail)
    ctx_lines = []
    if settings.ssh_host:
        ctx_lines.append(f"SSH target: {settings.ssh_host}")
    if settings.repo_dir:
        ctx_lines.append(f"Repo dir: {settings.repo_dir}")
    terminal_tail = (workspace.get("terminal_tail") or "").strip()
    if terminal_tail:
        tail_lines = terminal_tail.splitlines()[-200:]
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

    def _call():
        return Runner.run_sync(agent, prompt, **kwargs)

    future = EXECUTOR.submit(_call)
    try:
        result = future.result(timeout=25)
        _record_run_usage(result)
        return {"agent": name, "output": result.final_output}
    except FuturesTimeout:
        return {"agent": name, "error": "Agent timed out after 25 s. Please try again."}
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


def start_vibe_run(workdir: str, prompt: str) -> str:
    """Start a Vibe execution in a background thread. Returns run_id."""
    run_id = _uuid.uuid4().hex[:8]
    run: dict = {"status": "running", "output": "", "error": ""}
    with _VIBE_RUNS_LOCK:
        _VIBE_RUNS[run_id] = run

    def _execute() -> None:
        try:
            proc = subprocess.run(
                ["vibe", "--workdir", workdir, "--prompt", prompt],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
            output = (
                f"exit={proc.returncode}\n"
                f"STDOUT:\n{proc.stdout}\n"
                f"STDERR:\n{proc.stderr}"
            )
            with _VIBE_RUNS_LOCK:
                run["status"] = "done"
                run["output"] = output
        except subprocess.TimeoutExpired:
            with _VIBE_RUNS_LOCK:
                run["status"] = "error"
                run["error"] = "Vibe timed out after 900 seconds."
        except FileNotFoundError:
            with _VIBE_RUNS_LOCK:
                run["status"] = "error"
                run["error"] = (
                    "vibe executable not found. "
                    "Ensure Vibe is installed and available on PATH."
                )
        except Exception as exc:
            with _VIBE_RUNS_LOCK:
                run["status"] = "error"
                run["error"] = f"Vibe error ({type(exc).__name__}). Check server logs."

    _threading.Thread(target=_execute, daemon=True).start()
    return run_id


def get_vibe_run(run_id: str) -> dict:
    """Return the current status and output of a Vibe run."""
    with _VIBE_RUNS_LOCK:
        run = _VIBE_RUNS.get(run_id)
        if run is None:
            return {"status": "not_found", "output": "", "error": ""}
        return dict(run)