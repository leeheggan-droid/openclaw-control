from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

from agents import Agent, Runner, function_tool

from openclaw_control.tools.web_search import web_search


@function_tool
def run_vibe_report(report_id: str) -> str:
    """Fetch live system data from the VPS via SSH and return the raw results.

    Use this whenever you need real-time server evidence — do NOT ask the operator
    to paste logs or relay data manually; call this tool instead.

    Valid report_id values
    ----------------------
    container_health     — docker container status + restart counts
    last_trade           — last 10 executed trades (VPS SQLite then VPS docker logs fallback)
    trade_history_7d     — last 200 trades (VPS SQLite then VPS docker logs fallback)
    pnl_snapshot         — P&L, equity, drawdown, Sharpe snapshots
    halt_status          — HALT / risk / paused state from logs + container inspect
    per_trade_analytics  — gross/net P&L, fees, slippage, reason codes per trade
    git_head             — current git HEAD SHA on the VPS repo

    Returns the SSH output as plain text. If SSH is not configured, returns a
    message explaining that so you can inform the operator.
    """
    from openclaw_control.service import run_vibe_report as _run  # lazy — avoids circular load
    return _run(report_id)


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
        "You have four tools. Use them proactively — never ask the operator to do what a tool can do:\n"
        "\n"
        "1. run_vibe_report(report_id) — fetch live VPS system data via SSH directly\n"
        "   Use for: container health, last trades, P&L snapshots, halt status, git HEAD\n"
        "   Valid report_id values:\n"
        "     container_health     — docker container status + restart counts\n"
        "     last_trade           — last 10 executed trades\n"
        "     trade_history_7d     — last 200 trades\n"
        "     pnl_snapshot         — P&L, equity, drawdown, Sharpe\n"
        "     halt_status          — HALT / risk / paused state\n"
        "     per_trade_analytics  — gross/net P&L, fees, slippage, reason codes\n"
        "     git_head             — current git HEAD on the VPS repo\n"
        "   Example: run_vibe_report('container_health')\n"
        "\n"
        "2. ask_pnl(text)    — delegate to the P&L specialist agent\n"
        "   Use for: P&L totals, fees, trade attribution, Sharpe, drawdown analysis\n"
        "   Example: ask_pnl('What is the total net P&L for crypto and Alpaca trades?')\n"
        "\n"
        "3. ask_quant(text)  — delegate to the Quant specialist agent\n"
        "   Use for: strategy quality, backtesting critique, statistical validity\n"
        "   Example: ask_quant('Is the ORB strategy statistically sound given current win rate?')\n"
        "\n"
        "4. web_search(query) — search the web via Brave\n"
        "   Use for: live crypto prices, exchange status, news, docs\n"
        "   Example: web_search('BTC price today'), web_search('Kraken API status')\n"
        "\n"
        "═══════════════════════════════════\n"
        "RULES\n"
        "═══════════════════════════════════\n"
        "- NEVER say 'use the P&L tab' or 'switch to Quant' — call the tool yourself and report back.\n"
        "- NEVER ask the operator to paste logs, relay data, or relay command output.\n"
        "- NEVER fabricate metrics or trade outcomes.\n"
        "- NEVER execute SSH commands yourself — for pre-defined data pulls use run_vibe_report;\n"
        "  for ad-hoc operator-run commands propose them prefixed with '!'.\n"
        "- Budget constraints that apply to other agents do NOT apply to you.\n"
        "- Do NOT ask for P&L or trade data if the user is asking a general question — use judgment.\n"
        "- Keep responses concise unless the question warrants depth.\n"
        "\n"
        "═══════════════════════════════════\n"
        "TOOL SEQUENCING\n"
        "═══════════════════════════════════\n"
        "- For system health questions, start with run_vibe_report('container_health').\n"
        "- For trade/P&L questions, call run_vibe_report('last_trade') or run_vibe_report('pnl_snapshot')\n"
        "  then pass the raw data to ask_pnl for structured analysis.\n"
        "- For compound questions (e.g. 'total P&L for crypto AND Alpaca'), call ask_pnl once with the full question.\n"
        "- For questions needing both P&L and strategy insight, call ask_pnl then ask_quant.\n"
        "- For market context + system state, use web_search + run_vibe_report.\n"
        "- Always synthesise the tool responses into a single clear answer — don't just paste raw tool output.\n"
        "\n"
        f"{_REPO_CAPABILITY_MAP}\n"
        "\n"
        "═══════════════════════════════════\n"
        "VIBE / SHELL INTELLIGENCE\n"
        "═══════════════════════════════════\n"
        "Use run_vibe_report for pre-defined read-only probe sets (fastest, no operator action needed).\n"
        "Use '!' prefix for ad-hoc one-off commands the operator runs manually in the terminal.\n"
        "Use OPS_MAP_CORE_MEMORY (injected into every message) for exact command references.\n"
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
    tools=[run_vibe_report, ask_pnl, ask_quant, web_search],
)
