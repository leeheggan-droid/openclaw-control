from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

from agents import Agent, ModelSettings, Runner, function_tool
from openclaw_control.budget import COO_BUDGET_MESSAGE


def _run_agent_in_thread(agent, prompt: str, timeout: int = 25) -> str:
    """Run an agent synchronously in a fresh thread to avoid nested event-loop conflicts.

    The default timeout (25 s) gives sub-agents enough time to complete within
    COO's outer budget while still leaving headroom for the final synthesis.
    Sub-agent runs are capped at max_turns=1 (no tools) to prevent self-triggering loops.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(Runner.run_sync, agent, prompt, max_turns=1)
        try:
            return fut.result(timeout=timeout).final_output
        except _FuturesTimeout:
            return f"[{agent.name} timed out after {timeout}s]"
        except Exception as exc:
            return f"[{agent.name} error: {type(exc).__name__}: {exc}]"


@function_tool
def ask_pnl(text: str) -> str:
    """Query the P&L Agent with a finance, fees, or performance question.
    Returns the P&L Agent's structured analysis.
    Call this tool at most ONCE per request."""
    from openclaw_control.agents.pnl_agent import pnl_agent  # avoid circular import at module load

    return _run_agent_in_thread(pnl_agent, text)


@function_tool
def ask_quant(text: str) -> str:
    """Query the Quant Agent with a statistics, backtesting, or bot-engineering question.
    Returns the Quant Agent's structured analysis.
    Call this tool at most ONCE per request."""
    from openclaw_control.agents.quant_agent import quant_agent  # avoid circular import at module load

    return _run_agent_in_thread(quant_agent, text)


coo_agent = Agent(
    name="COO Agent",
    instructions=(
        "You are the OpenClaw COO — the orchestrator who synthesises P&L + Quant insights into "
        "practical decisions.\n"
        "Workspace context (SSH target, repo dir, last terminal output, OPS_MAP_CORE_MEMORY) is "
        "injected at the start of each message. The terminal output already contains recent logs — "
        "do NOT ask the user for more logs if terminal output is present.\n"
        "\n"
        "You have two optional tools:\n"
        "- ask_pnl(text): Query the P&L Agent for financial analysis.\n"
        "- ask_quant(text): Query the Quant Agent for statistical/methodological analysis.\n"
        "\n"
        "Strict rules:\n"
        "- For simple status queries ('you ok?', 'summarise state', etc.) answer directly from "
        "workspace context WITHOUT calling tools. Tools are for substantive analysis requests.\n"
        "- If you do call tools, call each at most ONCE per request. No repeat calls.\n"
        "- You MUST always produce a final decision memo, even if one or both tools return an "
        "error or timeout. In that case, note the failure and base your memo on available "
        "information.\n"
        "- Keep work practical and outcome-aligned. No over-engineering.\n"
        "- Do NOT execute SSH commands or suggest destructive actions.\n"
        "- Do NOT attempt to call yourself or any other orchestration agent.\n"
        "- NEVER ask the operator to paste logs, switch tabs, or relay data manually.\n"
        "- Use OPS_MAP_CORE_MEMORY to understand what data is available and where to look.\n"
        "- If the prompt begins with [BUDGET LOW], do NOT call ask_pnl or ask_quant. Instead, "
        f"respond immediately with:\n"
        f"  '{COO_BUDGET_MESSAGE}'\n"
        "\n"
        "Normal output structure:\n"
        "1) Decision memo: what we know / what we don't know\n"
        "2) Risks & constraints\n"
        "3) Next actions (max 3 items)\n"
        "4) If a code change is needed: a single /copilot-ready task sentence with acceptance criteria\n"
    ),
    model_settings=ModelSettings(max_tokens=800),
    tools=[ask_pnl, ask_quant],
)
