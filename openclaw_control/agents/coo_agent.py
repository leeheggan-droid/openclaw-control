from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

from agents import Agent, Runner, function_tool


def _run_agent_in_thread(agent, prompt: str, timeout: int = 20) -> str:
    """Run an agent synchronously in a fresh thread to avoid nested event-loop conflicts.

    The default timeout (20 s) is intentionally shorter than the outer 25 s hard timeout
    so the COO agent has time to process both sub-agent responses before its own deadline.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(Runner.run_sync, agent, prompt)
        try:
            return fut.result(timeout=timeout).final_output
        except _FuturesTimeout:
            return f"[{agent.name} timed out after {timeout}s]"
        except Exception as exc:
            return f"[{agent.name} error: {type(exc).__name__}: {exc}]"


@function_tool
def ask_pnl(text: str) -> str:
    """Query the P&L Agent with a finance, fees, or performance question.
    Returns the P&L Agent's structured analysis."""
    from openclaw_control.agents.pnl_agent import pnl_agent  # avoid circular import at module load

    return _run_agent_in_thread(pnl_agent, text)


@function_tool
def ask_quant(text: str) -> str:
    """Query the Quant Agent with a statistics, backtesting, or bot-engineering question.
    Returns the Quant Agent's structured analysis."""
    from openclaw_control.agents.quant_agent import quant_agent  # avoid circular import at module load

    return _run_agent_in_thread(quant_agent, text)


coo_agent = Agent(
    name="COO Agent",
    instructions=(
        "You are the OpenClaw COO — the orchestrator who synthesises P&L + Quant insights into "
        "practical decisions.\n"
        "Workspace context (SSH target, repo dir, last terminal output) is injected at the start of each message.\n"
        "\n"
        "You have two tools:\n"
        "- ask_pnl(text): Query the P&L Agent for financial analysis.\n"
        "- ask_quant(text): Query the Quant Agent for statistical/methodological analysis.\n"
        "\n"
        "Rules:\n"
        "- For every decision memo you MUST call BOTH ask_pnl and ask_quant "
        "before writing the final memo, so the memo explicitly references both viewpoints.\n"
        "- Keep work practical and outcome-aligned. No over-engineering.\n"
        "- Do NOT execute SSH commands or suggest destructive actions.\n"
        "\n"
        "Always structure your final output as:\n"
        "1) Decision memo: what we know / what we don't know\n"
        "2) Risks & constraints\n"
        "3) Next actions (max 3 items)\n"
        "4) If a code change is needed: a single /copilot-ready task sentence with acceptance criteria\n"
    ),
    tools=[ask_pnl, ask_quant],
)
