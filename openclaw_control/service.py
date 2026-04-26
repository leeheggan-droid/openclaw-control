import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from agents import Runner, SQLiteSession
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

    def _call():
        return Runner.run_sync(agent, prompt, session=session)

    future = EXECUTOR.submit(_call)
    try:
        result = future.result(timeout=25)
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