import subprocess
from agents import function_tool


@function_tool
def ssh_run(host: str, command: str) -> str:
    """
    Run a non-interactive, read-only SSH command.
    Designed to NEVER block or prompt.
    """
    proc = subprocess.run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=5",
            host,
            command,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )

    return (
        f"exit={proc.returncode}\n"
        f"STDOUT:\n{proc.stdout}\n"
        f"STDERR:\n{proc.stderr}"
    )