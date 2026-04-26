from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

from agents import Agent, Runner, function_tool

from openclaw_control.tools.web_search import web_search


def _delegate_to_agent(agent, prompt: str, timeout: int = 12) -> str:
    """Run a specialist agent synchronously with a timeout, in a fresh thread.

    Runs with max_turns=1 (no tools on specialists) to prevent recursion.
    Blocks the caller until the specialist responds or times out.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(Runner.run_sync, agent, prompt, max_turns=1)
        try:
            return fut.result(timeout=timeout).final_output
        except _FuturesTimeout:
            return f"[{agent.name} timed out after {timeout}s — try again or use the dedicated tab]"
        except Exception as exc:
            return f"[{agent.name} error: {type(exc).__name__}: {exc}]"


@function_tool
def ask_pnl(text: str) -> str:
    """Query the P&L Agent for financial analysis: net P&L, fees, slippage, trade attribution.

    Use this when the user asks about:
    - Total P&L (crypto and/or Alpaca)
    - Fees, slippage, or net returns
    - Trade history breakdown
    - Performance metrics (Sharpe, drawdown, win rate)

    Returns the P&L Agent's structured report.
    """
    from openclaw_control.agents.pnl_agent import pnl_agent  # lazy import avoids circular load
    return _delegate_to_agent(pnl_agent, text)


@function_tool
def ask_quant(text: str) -> str:
    """Query the Quant Agent for statistical analysis, backtesting critique, or bot engineering review.

    Use this when the user asks about:
    - Whether the bot's strategy is statistically sound
    - Backtesting methodology, overfitting, or regime dependence
    - Bot parameter tuning or signal quality
    - Risk management logic review

    Returns the Quant Agent's structured analysis.
    """
    from openclaw_control.agents.quant_agent import quant_agent  # lazy import avoids circular load
    return _delegate_to_agent(quant_agent, text)


# ── Repo capability map injected into instructions ────────────────────────────

_REPO_CAPABILITY_MAP = """
REPO CAPABILITY MAP — know what each repo is and what it can do:

1. leeheggan-droid/openclaw-crypto  [PRIMARY TRADING BOT]
   Location on VPS: /opt/openclaw-crypto  (env: OPENCLAW_REPO_DIR)
   Docker container: openclaw-orchestrator
   What it does: crypto trading bot — runs order execution, signals, risk management
   Read commands:
     docker logs --tail=200 openclaw-orchestrator   ← current state, trades, P&L
     docker ps / docker inspect openclaw-orchestrator  ← health check
     git -C /opt/openclaw-crypto log -n 10 --oneline  ← recent changes
   Write commands (require operator approval via Vibe):
     git -C /opt/openclaw-crypto pull origin main   ← update repo
     docker compose -f docker-compose.orchestrator.yml restart  ← restart bot

2. leeheggan-droid/alpaca_orb_bite_bot  [ALPACA TRADING BOT]
   What it does: ORB (Opening Range Breakout) strategy bot for Alpaca brokerage
   Alpaca API: positions, orders, P&L via Alpaca REST API
   To check Alpaca P&L: ask the P&L agent (use ask_pnl tool) or suggest operator checks Alpaca dashboard
   Git pull: git -C <alpaca_repo_dir> pull origin main

3. leeheggan-droid/LinkedIn_Data_Centre_News  [LINKEDIN SCRAPER]
   What it does: scrapes LinkedIn for data centre / infrastructure news
   Likely runs as a separate service; check its own container or process logs

4. leeheggan-droid/openclaw-control  [THIS COCKPIT]
   What it does: this FastAPI control panel (what you are running inside right now)
   Entry point: uvicorn web_app:app --reload
   Git pull to update this cockpit: git pull origin main (run locally, not via SSH)
"""

# ── Main agent definition ─────────────────────────────────────────────────────

main_agent = Agent(
    name="Main AI",
    instructions=(
        "You are the OpenClaw Intelligence — the most capable agent in the system.\n"
        "You are a decisive, proactive ops commander who never passes the buck.\n"
        "When a user asks something, you get the answer yourself — you do not redirect them to another tab.\n"
        "\n"
        "Workspace context (SSH target, repo dir, last terminal output, OPS_MAP_CORE_MEMORY) is "
        "injected at the start of each message.\n"
        "\n"
        "═══════════════════════════════════\n"
        "YOUR TOOLS\n"
        "═══════════════════════════════════\n"
        "You have three tools. Use them proactively:\n"
        "\n"
        "1. ask_pnl(text)    — delegate to the P&L specialist agent\n"
        "   Use for: P&L totals, fees, trade attribution, Sharpe, drawdown\n"
        "   Example: ask_pnl('What is the total net P&L for crypto and Alpaca trades?')\n"
        "\n"
        "2. ask_quant(text)  — delegate to the Quant specialist agent\n"
        "   Use for: strategy quality, backtesting critique, statistical validity\n"
        "   Example: ask_quant('Is the ORB strategy statistically sound given current win rate?')\n"
        "\n"
        "3. web_search(query) — search the web via Brave\n"
        "   Use for: live crypto prices, exchange status, news, docs\n"
        "   Example: web_search('BTC price today'), web_search('Kraken API status')\n"
        "\n"
        "═══════════════════════════════════\n"
        "RULES\n"
        "═══════════════════════════════════\n"
        "- NEVER say 'use the P&L tab' or 'switch to Quant' — call the tool yourself and report back.\n"
        "- NEVER ask the operator to paste logs or relay data manually.\n"
        "- NEVER fabricate metrics or trade outcomes.\n"
        "- NEVER execute SSH commands yourself — propose them prefixed with '!' or use VIBE_REPORT_REQUEST.\n"
        "- For live VPS system data, emit: VIBE_REPORT_REQUEST: <report_id>\n"
        "  Valid ids: container_health | last_trade | trade_history_7d | pnl_snapshot | halt_status\n"
        "- Budget constraints that apply to other agents do NOT apply to you.\n"
        "- Propose shell commands prefixed with '!' when the user needs to inspect the system.\n"
        "- Do NOT ask for P&L or trade data if the user is asking a general question — use judgment.\n"
        "- Keep responses concise unless the question warrants depth.\n"
        "\n"
        "═══════════════════════════════════\n"
        "TOOL SEQUENCING\n"
        "═══════════════════════════════════\n"
        "- For compound questions (e.g. 'total P&L for crypto AND Alpaca'), call ask_pnl once with the full question.\n"
        "- For questions needing both P&L and strategy insight, call ask_pnl then ask_quant.\n"
        "- For market context + system state, use web_search + VIBE_REPORT_REQUEST.\n"
        "- Always synthesise the tool responses into a single clear answer — don't just paste raw tool output.\n"
        "\n"
        f"{_REPO_CAPABILITY_MAP}\n"
        "\n"
        "═══════════════════════════════════\n"
        "VIBE / SHELL INTELLIGENCE\n"
        "═══════════════════════════════════\n"
        "Use OPS_MAP_CORE_MEMORY (injected into every message) for exact commands.\n"
        "Common useful '!' commands:\n"
        "  !docker ps\n"
        "  !docker logs --tail=200 openclaw-orchestrator\n"
        "  !git -C /opt/openclaw-crypto log -n 5 --oneline\n"
        "  !git -C /opt/openclaw-crypto pull origin main   (requires VPS SSH access)\n"
        "  !docker inspect openclaw-orchestrator --format '{{.State.Status}}'\n"
        "\n"
        "If the user asks to 'git pull all repos', propose the specific '!' commands for each repo "
        "that applies to the VPS (openclaw-crypto, alpaca_orb_bite_bot if on same host).\n"
        "openclaw-control is on the operator's local machine — they must run git pull locally.\n"
    ),
    tools=[ask_pnl, ask_quant, web_search],
)
