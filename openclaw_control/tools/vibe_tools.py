from agents import function_tool

from openclaw_control.tools.ssh_tools import ssh_run
from openclaw_control.config import settings


@function_tool(needs_approval=True)
def vibe_ssh_run(command: str) -> str:
    """Run an approved shell command on the VPS via SSH (approval required)."""
    if not settings.ssh_host:
        return "error: OPENCLAW_SSH_HOST is not configured."
    return ssh_run(settings.ssh_host, command)