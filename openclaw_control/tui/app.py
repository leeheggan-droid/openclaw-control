from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Input, RichLog
from textual.containers import Vertical
from textual import work

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

from agents import Runner, SQLiteSession
from openclaw_control.config import settings
from openclaw_control.agents.main_agent import main_agent
from openclaw_control.agents.analysis_agent import analysis_agent
from openclaw_control.agents.router import route_message

EXECUTOR = ThreadPoolExecutor(max_workers=2)

class OpenClawTUI(App):
    CSS = """
    Screen { layout: vertical; }
    RichLog { height: 1fr; }
    """

    def __init__(self):
        super().__init__()
        self.session_main = SQLiteSession("openclaw_main_session")
        self.session_analysis = SQLiteSession("openclaw_analysis_session")

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield RichLog(id="log", markup=True)
            yield Input(placeholder="Ask agent, or run direct SSH with ! (e.g. !uptime)")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one(RichLog)
        log.write("[bold green]OpenClaw Control ready.[/bold green]")
        log.write(f"[dim]SSH host (Vibe):[/dim] {settings.ssh_host}")
        log.write(f"[dim]SSH host (READONLY):[/dim] {settings.ssh_readonly_host or '(not configured)'}")
        log.write(f"[dim]Repo:[/dim] {settings.repo_dir}")
        log.write("[dim]Direct SSH:[/dim] prefix commands with !")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return

        log = self.query_one(RichLog)
        log.write(f"[bold cyan]You:[/bold cyan] {text}")

        if text.startswith("!"):
            cmd = text[1:].strip()
            self.run_ssh(cmd)
            return

        route = route_message(text)
        if route.name == "analysis":
            self.run_agent(text, analysis_agent, self.session_analysis, "Analysis")
        else:
            self.run_agent(text, main_agent, self.session_main, "Main")

    @work(thread=True)
    def run_ssh(self, cmd: str) -> None:
        log = self.query_one(RichLog)
        log.write(f"[italic]SSH:[/italic] {cmd}")

        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", settings.ssh_host, cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )
            out = proc.stdout.strip()
            err = proc.stderr.strip()
            if out:
                log.write(out)
            if err:
                log.write(f"[red]{err}[/red]")
        except Exception as e:
            log.write(f"[bold red]SSH error:[/bold red] {e}")

    @work(thread=True)
    def run_agent(self, text: str, agent, session, label: str) -> None:
        log = self.query_one(RichLog)
        log.write(f"[italic yellow]Thinking ({label})…[/italic yellow]")

        def _call():
            prompt = (
                f"Host={settings.ssh_host}\n"
                f"Repo={settings.repo_dir}\n"
                f"{text}"
            )
            return Runner.run_sync(agent, prompt, session=session)

        future = EXECUTOR.submit(_call)

        # Heartbeat: write a progress line every 30 s while the agent is running
        stop_event = threading.Event()

        def _heartbeat():
            elapsed = 0
            while not stop_event.wait(30):
                elapsed += 30
                self.call_from_thread(
                    log.write,
                    f"[dim]⏳ Still working ({label}, {elapsed}s)…[/dim]",
                )

        hb_thread = threading.Thread(target=_heartbeat, daemon=True)
        hb_thread.start()

        try:
            result = future.result(timeout=300)
            log.write(f"[bold magenta]{label}:[/bold magenta] {result.final_output}")
        except FuturesTimeout:
            log.write(f"[bold red]{label} timed out[/bold red] (300s). Try again or use ! commands.")
        except Exception as e:
            log.write(f"[bold red]{label} error:[/bold red] {e}")
        finally:
            stop_event.set()