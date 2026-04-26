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
        "docker inspect --format '{{.Name}} state={{.State.Status}} restarts={{.RestartCount}}' <container>",
        "docker logs --tail=<N> <container>",
        "docker logs --tail=<N> <container> 2>&1 | grep -iE '<pattern>'",
        "docker stats --no-stream --format 'table {{.Name}}\\t{{.CPUPerc}}\\t{{.MemUsage}}'",
    ],
    "trade_logs": [
        "find <repo> -maxdepth 5 -name 'trade*.csv' -o -name '*trades*.csv'",
        "find <repo> -maxdepth 5 -name 'pnl*.csv' -o -name '*pnl*.csv'",
        "tail -n <N> <trade_log_file>",
        "docker logs --tail=<N> <container> | grep -iE 'trade|pnl|signal'",
    ],
    "paths": [
        "find <repo> -maxdepth 3 -name '*.env' -o -name '*.yml' -o -name '*.yaml'",
        "find <repo> -maxdepth 4 -name '*.py' -o -name '*.json' -o -name '*.toml'",
        "ls -la <repo>",
        "ls -la <directory>",
        "cat <repo>/<file>",
        "head -n <N> <file>",
        "tail -n <N> <file>",
        "git -C <repo> log -n <N> --oneline",
        "git -C <repo> diff HEAD~1 --stat",
        "git -C <repo> status",
        "uptime",
    ],
    "system": [
        "uname -a",
        "df -h",
        "free -h",
        "ps aux --sort=-%cpu | head -n 20",
        "ps aux --sort=-%mem | head -n 20",
        "top -bn1 | head -n 20",
        "cat /proc/loadavg",
        "uptime",
        "who",
        "last -n 10",
    ],
    "network": [
        "ss -tlnp",
        "ss -s",
        "netstat -tlnp 2>/dev/null || ss -tlnp",
        "curl -s --max-time 5 http://localhost:<port>/health",
        "curl -s --max-time 5 http://localhost:<port>/",
        "ping -c 3 <host>",
        "cat /etc/hosts",
    ],
    "services": [
        "systemctl status <service>",
        "systemctl list-units --type=service --state=running",
        "systemctl list-units --type=service --state=failed",
        "journalctl -n <N> -u <service> --no-pager",
        "journalctl --since '1 hour ago' --no-pager | tail -n 100",
        "cat /etc/systemd/system/<service>.service",
    ],
    "environment": [
        "printenv | grep -iE '<pattern>'",
        "cat <repo>/.env 2>/dev/null | grep -v '^#' | grep -v '^$'",
        "cat <file> | grep -v 'SECRET\\|PASSWORD\\|KEY\\|TOKEN'",
        "env | sort",
    ],
    "disk": [
        "df -h",
        "du -sh <directory>",
        "du -sh <repo>/*",
        "ls -lhS <directory> | head -n 20",
        "find <repo> -name '*.log' -exec ls -lh {} \\;",
    ],
    "processes": [
        "pgrep -a python",
        "pgrep -a node",
        "pgrep -a <name>",
        "ps aux | grep <pattern>",
        "lsof -i :<port>",
    ],
}


@function_tool(needs_approval=True)
def vibe_ssh_run(command: str) -> str:
    """Run an approved shell command on the VPS via SSH (approval required)."""
    if not settings.ssh_host:
        return "error: OPENCLAW_SSH_HOST is not configured."
    return ssh_run(settings.ssh_host, command)