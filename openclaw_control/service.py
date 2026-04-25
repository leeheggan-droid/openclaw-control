import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from agents import Runner, SQLiteSession
from openclaw_control.config import settings
from openclaw_control.agents.controller import controller
from openclaw_control.agents.analysis_agent import analysis_agent
from openclaw_control.agents.router import route_message

EXECUTOR = ThreadPoolExecutor(max_workers=2)

_ops_session = SQLiteSession("openclaw_ops_session")
_analysis_session = SQLiteSession("openclaw_analysis_session")


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
            "error": str(e),
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
            "error": str(e),
        }


def handle_message(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"type": "noop"}

    if text.startswith("!"):
        return run_ssh(text[1:].strip())

    return run_agent(text)