import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from agents import Runner, RunResult, SQLiteSession
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

EXECUTOR = ThreadPoolExecutor(max_workers=2)

_ops_session = SQLiteSession("openclaw_ops_session")
_analysis_session = SQLiteSession("openclaw_analysis_session")
_main_session = SQLiteSession("openclaw_main_session")
_pnl_session = SQLiteSession("openclaw_pnl_session")
_quant_session = SQLiteSession("openclaw_quant_session")
_coo_session = SQLiteSession("openclaw_coo_session")


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
    "main":  (main_agent,  _main_session),
    "pnl":   (pnl_agent,   _pnl_session),
    "quant": (quant_agent, _quant_session),
    "coo":   (coo_agent,   _coo_session),
}

# Per-agent max_turns limits.
# Main is unbounded (uses Runner default).
# Specialists (no tools) need only 1 turn; COO needs up to 3 (tool × 2 + final memo).
_MAX_TURNS: dict[str, int | None] = {
    "main":  None,   # unbounded
    "pnl":   1,
    "quant": 1,
    "coo":   3,
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