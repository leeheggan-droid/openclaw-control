from agents import function_tool

from openclaw_control.tools.ssh_tools import ssh_run
from openclaw_control.config import settings

# Explicit read-only capability map for Vibe.
# Lists every category of data Vibe is permitted to fetch via SSH.
# Agents and orchestration code MUST consult this map before assuming
# data is available — if a category is absent here, Vibe cannot provide it.
VIBE_CAPABILITY_MAP: dict[str, list[str]] = {
    "containers": [
        "docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.State}}'",
        "docker inspect --format '{{.Name}} state={{.State.Status}}' <container>",
        "docker logs --tail=<N> <container>",
    ],
    "trade_logs": [
        "find <repo> -maxdepth 5 -name 'trade*.csv' -o -name '*trades*.csv'",
        "find <repo> -maxdepth 5 -name 'pnl*.csv' -o -name '*pnl*.csv'",
        "tail -n <N> <trade_log_file>",
        "docker logs --tail=<N> <container> | grep -iE 'trade|pnl|signal'",
    ],
    "paths": [
        "find <repo> -maxdepth 3 -name '*.env' -o -name '*.yml' -o -name '*.yaml'",
        "git -C <repo> log -n <N> --oneline",
        "uptime",
    ],
}


@function_tool(needs_approval=True)
def vibe_ssh_run(command: str) -> str:
    """Run an approved shell command on the VPS via SSH (approval required)."""
    if not settings.ssh_host:
        return "error: OPENCLAW_SSH_HOST is not configured."
    return ssh_run(settings.ssh_host, command)