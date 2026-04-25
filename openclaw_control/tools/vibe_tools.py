import subprocess
from agents import function_tool


@function_tool(needs_approval=True)
def vibe_prompt(workdir: str, prompt: str) -> str:
    """Ask Vibe to draft code changes (approval required)."""
    proc = subprocess.run(
        ["vibe", "--workdir", workdir, "--prompt", prompt],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    return (
        f"exit={proc.returncode}\n"
        f"STDOUT:\n{proc.stdout}\n"
        f"STDERR:\n{proc.stderr}"
    )