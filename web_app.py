import json
import logging
import os

from dotenv import load_dotenv

load_dotenv("/etc/openclaw-control.env", override=True)

import requests as _requests

from contextlib import asynccontextmanager

from fastapi import Cookie, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import auth_feature as _auth
import chat_feature as _chat
import multi_chat_feature as _multi_chat

from cheap_chat_feature import cheap_chat as _cheap_chat
from openclaw_control.config import settings
from openclaw_control.github_tools import (
    ALLOWED_REPOS,
    gh_headers as _gh_headers,
    try_assign_copilot as _try_assign_copilot,
    create_github_issue,
)
from openclaw_control.ops import map_loader as _map_loader
from openclaw_control.memory import agent_memory as _memory
from openclaw_control.service import (
    handle_message, handle_agent_message,
    run_vibe_report,
    run_ssh_readonly,
    start_team_review, get_team_review_events,
)
import adapters.vps_wrappers as _vps
from openclaw_control import trade_log as _trade_log
from openclaw_control.trade_log import now_iso as _trade_log_now_iso
from openclaw_control.tools.exchange_tools import (
    fetch_kraken_open_positions as _fetch_kraken_positions,
    fetch_kraken_trade_balance as _fetch_kraken_balance,
    fetch_kraken_trades as _fetch_kraken_trades,
    fetch_alpaca_open_positions as _fetch_alpaca_positions,
    fetch_alpaca_account as _fetch_alpaca_account,
    fetch_alpaca_trades as _fetch_alpaca_trades,
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start background services when the server boots."""
    _trade_log.start_scheduler(
        pnl_probe_fn=lambda: run_vibe_report("pnl_snapshot"),
    )
    yield


app = FastAPI(lifespan=_lifespan)

_logger = logging.getLogger(__name__)

# Maximum length for error messages returned to API clients.
_MAX_ERROR_MSG_LEN = 200


def _safe_error_msg(raw: str) -> str:
    """Return a client-safe error message.

    Credentials-not-set messages (which do not contain exception data) are
    returned as a generic 'not configured' notice.  All other errors return a
    fixed generic string so that internal details are never leaked to clients.
    The full message is always logged before calling this function.
    """
    _CRED_MISSING_PREFIXES = (
        "KRAKEN_API_KEY",
        "ALPACA_API_KEY",
        "KRAKEN_SECRET_KEY",
        "ALPACA_SECRET_KEY",
    )
    if any(raw.startswith(p) for p in _CRED_MISSING_PREFIXES):
        return "Exchange API credentials not configured — set the required environment variables."
    return "Exchange API error — check server logs for details."


class Message(BaseModel):
    text: str



class CopilotRequest(BaseModel):
    goal: str
    last_user_msg: str = ""
    last_agent_response: str = ""
    shell_output: str = ""
    target_repo: str = ""  # empty string means "use server default"


class AgentMessage(BaseModel):
    agent: str
    text: str
    workspace: dict = {}


class TeamReviewRequest(BaseModel):
    mode: str = "quick"
    prompt: str = ""
    workspace: dict = {}
    review_period: str = ""


class ProposalConfirmRequest(BaseModel):
    title: str
    body: str
    repo: str
    labels: list[str] = []
    assign_copilot: bool = True


class CheapChatRequest(BaseModel):
    message: str
    provider: str = "groq"
    history: list[dict] = []  # list of {role, content} dicts


class TradeLogRequest(BaseModel):
    ts: str = ""          # ISO-8601 UTC; server time used when empty
    symbol: str
    side: str             # 'buy' | 'sell'
    size: float
    fill_price: float
    trade_id: str = ""
    source: str = "api"
    # Extended unified schema fields
    exchange: str = ""
    open_ts: str = ""
    close_ts: str = ""
    entry_price: float | None = None
    exit_price: float | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None
    fee: float | None = None
    tag: str = ""
    signal: str = ""
    strategy: str = ""
    config_version: str = ""
    annotation: str = ""


class TradeTagRequest(BaseModel):
    tag: str              # 'good' | 'bad' | 'neutral' | ''
    annotation: str = ""  # optional free-text reason


class PnlLogRequest(BaseModel):
    ts: str = ""          # ISO-8601 UTC; server time used when empty
    total_pnl: float | None = None
    equity: float | None = None
    drawdown: float | None = None
    realised_pnl: float | None = None
    unrealised_pnl: float | None = None
    sharpe_ratio: float | None = None
    source: str = "api"


def _build_issue_body(req: CopilotRequest) -> str:
    goal = req.goal or "(no goal specified)"
    last_user = req.last_user_msg or "(none)"
    last_agent = req.last_agent_response or "(none)"

    shell_lines = (req.shell_output or "").splitlines()[-200:]
    shell = "\n".join(shell_lines) or "(no shell output)"

    lines = [
        "## Goal",
        goal,
        "",
        "## Context",
        f"- **Host (Vibe):** {settings.ssh_host or '(not configured)'}",
        f"- **Host (READONLY):** {settings.ssh_readonly_host or '(not configured)'}",
        f"- **Repo:** {settings.repo_dir or '(not configured)'}",
        "- **Triggered from:** OpenClaw Web UI",
        "",
        "## Reproduction steps (if fixing a bug)",
        "**Last user message:**",
        "```",
        last_user,
        "```",
        "",
        "**Last agent response:**",
        "```",
        last_agent,
        "```",
        "",
        "## Constraints",
        "- No secrets or credentials added to source code",
        "- No destructive operations introduced",
        "- Changes limited to the minimum required by the issue",
        "- Match existing code style and conventions",
        "",
        "## Acceptance criteria",
        "- [ ] Goal achieved as described above",
        "- [ ] Local test passed: `git pull; uvicorn web_app:app --reload;` then verified in browser",
        "",
        "## Shell Output (last 200 lines)",
        "```",
        shell,
        "```",
        "",
        "## Local test checklist",
        "- [ ] `git pull`",
        "- [ ] `uvicorn web_app:app --reload`",
        "- [ ] Verified expected behaviour in browser",
    ]
    return "\n".join(lines)


@app.get("/config")
def config(openclaw_session: str | None = Cookie(default=None)):
    if not _current_user(openclaw_session):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {
        "ssh_host": settings.ssh_host,
        "ssh_readonly_host": settings.ssh_readonly_host,
        "repo_dir": settings.repo_dir,
        "allowed_repos": sorted(ALLOWED_REPOS),
    }


# ── Ops map endpoint ──────────────────────────────────────────────────────────

@app.get("/ops/map")
def ops_map():
    """Return the ops map top-level keys and compressed summary.

    No secrets are included — this is a structural/contract map only.
    """
    try:
        top_keys = _map_loader.get_top_keys()
    except Exception:
        top_keys = []
    return {
        "top_level_keys": top_keys,
        "summary": _map_loader.get_summary(),
    }


_VALID_REPORT_IDS = frozenset({"container_health", "last_trade", "trade_history_7d", "pnl_snapshot", "halt_status", "git_head", "per_trade_analytics"})

_VALID_MEMORY_AGENTS = frozenset({"pnl", "quant", "coo"})


@app.post("/ops/report")
def ops_report(report_id: str):
    """Execute the read-only SSH probe sequence for *report_id* and return results.

    Valid report_ids: container_health | last_trade | trade_history_7d | pnl_snapshot | halt_status | git_head | per_trade_analytics
    """
    if report_id.lower() not in _VALID_REPORT_IDS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid report_id '{report_id}'. Valid values: {sorted(_VALID_REPORT_IDS)}",
        )
    output = run_vibe_report(report_id)
    return {"report_id": report_id, "output": output}


# Maximum command length accepted by the read-only SSH endpoint.
_READONLY_CMD_MAX_LEN = 512


class SshCommandRequest(BaseModel):
    cmd: str


# Alias kept for the read-only endpoint that predates this rename.
ReadonlySshRequest = SshCommandRequest


@app.post("/ops/ssh-readonly-run")
def ops_ssh_readonly_run(req: ReadonlySshRequest):
    """Run a single read-only command on the READONLY SSH host (operator-approved).

    The client must display the literal ssh command and receive explicit operator
    confirmation (approval banner) before calling this endpoint.

    The remote host enforces a first-token allowlist via bin/vibe-readonly-wrapper.sh.
    This endpoint performs a length check only — content restriction is server-side.
    """
    cmd = (req.cmd or "").strip()
    if not cmd:
        raise HTTPException(status_code=422, detail="cmd must not be empty")
    if len(cmd) > _READONLY_CMD_MAX_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"cmd too long (max {_READONLY_CMD_MAX_LEN} chars)",
        )
    if not settings.ssh_readonly_host:
        raise HTTPException(
            status_code=503,
            detail=(
                "OPENCLAW_SSH_READONLY_HOST is not configured — "
                "read-only SSH features are disabled."
            ),
        )
    result = run_ssh_readonly(cmd, timeout=30)
    return result


# ── Agent memory endpoints ─────────────────────────────────────────────────────

@app.get("/memory/{agent}")
def memory_get(agent: str):
    """Return the evidence-based memory snapshot for *agent*.

    Only derived summaries and flags are returned — no raw logs, no secrets.
    Valid agents: pnl | quant | coo
    """
    if agent.lower() not in _VALID_MEMORY_AGENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown agent '{agent}'. Valid values: {sorted(_VALID_MEMORY_AGENTS)}",
        )
    snap, fp = _memory.load_snapshot_with_fingerprint(agent.lower())
    events = _memory.get_events(agent.lower(), limit=10)
    return {
        "agent": agent.lower(),
        "snapshot": snap,
        "fingerprint": fp[:12] + "…" if fp else "",
        "recent_events": events,
    }


@app.post("/memory/{agent}/invalidate")
def memory_invalidate(agent: str, reason: str = "operator manual invalidation"):
    """Invalidate (clear) the memory snapshot for *agent*.

    Useful after a container restart, deployment, or when you want the agent to
    re-probe instead of reusing stale evidence.
    Valid agents: pnl | quant | coo
    """
    if agent.lower() not in _VALID_MEMORY_AGENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown agent '{agent}'. Valid values: {sorted(_VALID_MEMORY_AGENTS)}",
        )
    _memory.invalidate(agent.lower(), reason=reason[:200])
    return {"agent": agent.lower(), "status": "invalidated", "reason": reason[:200]}


class MemoryUpdateRequest(BaseModel):
    snapshot: dict


_MAX_MEMORY_SNAPSHOT_BYTES = 16_384  # 16 KB hard cap


@app.patch("/memory/{agent}")
def memory_update(agent: str, req: MemoryUpdateRequest):
    """Replace the memory snapshot for *agent* from the operator cockpit.

    Only derived summaries and flags — never secrets, never raw logs.
    Values may be strings, numbers, or booleans.
    Valid agents: pnl | quant | coo
    """
    if agent.lower() not in _VALID_MEMORY_AGENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown agent '{agent}'. Valid values: {sorted(_VALID_MEMORY_AGENTS)}",
        )
    payload_bytes = len(json.dumps(req.snapshot, ensure_ascii=False).encode())
    if payload_bytes > _MAX_MEMORY_SNAPSHOT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Snapshot too large ({payload_bytes} bytes). "
                f"Maximum is {_MAX_MEMORY_SNAPSHOT_BYTES} bytes."
            ),
        )
    fp = _memory.compute_fingerprint()
    _memory.save_snapshot(agent.lower(), req.snapshot, fp)
    return {
        "agent": agent.lower(),
        "status": "updated",
        "keys": list(req.snapshot.keys()),
    }


@app.get("/", response_class=HTMLResponse)
def index(openclaw_session: str | None = Cookie(default=None)):
    email = _current_user(openclaw_session)
    if not email:
        return RedirectResponse("/login", status_code=303)
    gateway_url = os.environ.get("OPENCLAW_GATEWAY_URL", "").strip()
    gateway_script = (
        f'<script>window.OPENCLAW_GATEWAY_URL = {json.dumps(gateway_url)};</script>\n'
        if gateway_url else ""
    )
    page = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>OpenClaw Control</title>
  <style>
    :root{
      --bg:#0b0f14;
      --panel:#0f1621;
      --panel2:#0c121b;
      --border:rgba(255,255,255,.08);
      --text:#e6eefc;
      --muted:rgba(230,238,252,.65);
      --accent:#22c55e;          /* green */
      --accent2:#16a34a;         /* deeper green */
      --bubbleUser:#1d2a3a;
      --bubbleAgent:#101b27;
      --shadow: 0 10px 30px rgba(0,0,0,.35);
      --radius:18px;
      --radius2:14px;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono","Courier New", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji","Segoe UI Emoji";
    }

    html,body{height:100%;}
    body{
      margin:0;
      font-family:var(--sans);
      background: radial-gradient(1200px 600px at 30% 0%, rgba(34,197,94,.10), transparent 55%),
                  radial-gradient(900px 600px at 85% 20%, rgba(59,130,246,.08), transparent 55%),
                  var(--bg);
      color:var(--text);
      overflow:hidden;
    }

    .app{
      height:100vh;
      display:grid;
      grid-template-columns: 1.2fr .8fr;
      gap:14px;
      padding:14px;
      box-sizing:border-box;
    }

    .card{
      background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.015));
      border:1px solid var(--border);
      border-radius:24px;
      box-shadow: var(--shadow);
      overflow:hidden;
      display:flex;
      flex-direction:column;
      min-width:0;
    }

    .cardHeader{
      padding:14px 16px;
      border-bottom:1px solid var(--border);
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:12px;
      background: rgba(255,255,255,.02);
    }
    .title{
      font-weight:650;
      letter-spacing:.2px;
      display:flex;
      align-items:center;
      gap:10px;
    }
    .badge{
      font-size:12px;
      padding:4px 10px;
      border-radius:999px;
      border:1px solid var(--border);
      color:var(--muted);
      background: rgba(0,0,0,.15);
    }

    /* LEFT: chat */
    .chatBody{
      flex:1;
      overflow:auto;
      padding:14px 14px 0 14px;
      scroll-behavior:smooth;
    }
    .chatBody::-webkit-scrollbar{width:10px;}
    .chatBody::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08); border-radius:999px;}

    .msgRow{
      display:flex;
      margin:10px 0;
      gap:10px;
      align-items:flex-end;
    }
    .msgRow.user{justify-content:flex-end;}
    .msgRow.agent{justify-content:flex-start;}

    .bubble{
      max-width: 78%;
      padding:12px 14px;
      border-radius: var(--radius);
      border:1px solid var(--border);
      background: var(--bubbleAgent);
      white-space:pre-wrap;
      line-height:1.35;
      font-size:14px;
    }
    .user .bubble{
      background: var(--bubbleUser);
      border-top-right-radius:8px;
    }
    .agent .bubble{
      background: var(--bubbleAgent);
      border-top-left-radius:8px;
    }
    .meta{
      font-size:12px;
      color:var(--muted);
      margin:0 6px 2px 6px;
      user-select:none;
    }

    .attachment{
      margin-top:10px;
      padding:10px 12px;
      border-radius: var(--radius2);
      border:1px dashed rgba(255,255,255,.15);
      background: rgba(0,0,0,.18);
      color: var(--muted);
      font-family: var(--mono);
      font-size:12px;
      overflow:auto;
      max-height:180px;
    }

    .imgPreview{
      margin-top:10px;
      border-radius: var(--radius2);
      border:1px solid var(--border);
      overflow:hidden;
      background: rgba(0,0,0,.25);
    }
    .imgPreview img{display:block; max-width:100%; height:auto;}

    .chatComposer{
      padding:12px;
      border-top:1px solid var(--border);
      background: rgba(255,255,255,.02);
    }
    .composerRow{
      display:flex;
      align-items:flex-end;
      gap:10px;
    }
    .inputWrap{
      flex:1;
      background: rgba(0,0,0,.20);
      border:1px solid var(--border);
      border-radius: 22px;
      padding:10px 12px;
      display:flex;
      gap:10px;
      align-items:flex-end;
    }
    textarea{
      flex:1;
      resize:none;
      border:none;
      outline:none;
      background:transparent;
      color:var(--text);
      font-family:var(--sans);
      font-size:14px;
      line-height:1.35;
      min-height:22px;
      max-height:130px;
      overflow:auto;
    }
    textarea::-webkit-scrollbar{width:10px;}
    textarea::-webkit-scrollbar-thumb{background:rgba(255,255,255,.10); border-radius:999px;}

    .iconBtn{
      width:38px;
      height:38px;
      border-radius:999px;
      border:1px solid var(--border);
      background: rgba(255,255,255,.03);
      cursor:pointer;
      display:grid;
      place-items:center;
      transition: transform .05s ease, background .15s ease;
    }
    .iconBtn:hover{background: rgba(255,255,255,.06);}
    .iconBtn:active{transform: scale(.98);}

    .sendBtn{
      width:44px;
      height:44px;
      border-radius:999px;
      border:1px solid rgba(34,197,94,.45);
      background: linear-gradient(180deg, rgba(34,197,94,.95), rgba(22,163,74,.95));
      cursor:pointer;
      display:grid;
      place-items:center;
      transition: transform .05s ease, filter .15s ease;
    }
    .sendBtn:hover{filter: brightness(1.05);}
    .sendBtn:active{transform: scale(.98);}
    .sendBtn svg{filter: drop-shadow(0 6px 10px rgba(0,0,0,.25));}

    .hintBar{
      margin-top:8px;
      display:flex;
      justify-content:space-between;
      gap:10px;
      color:var(--muted);
      font-size:12px;
      user-select:none;
    }
    .hintBar code{font-family:var(--mono); color:rgba(230,238,252,.85);}

    /* RIGHT: terminal */
    .termBody{
      flex:1;
      padding:14px;
      overflow:auto;
      background: #060a0f;
      font-family: var(--mono);
      font-size:13px;
      line-height:1.35;
    }
    .termBody::-webkit-scrollbar{width:10px;}
    .termBody::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08); border-radius:999px;}

    .termLine{white-space:pre-wrap;}
    .prompt{color:#93c5fd;}         /* blue-ish prompt */
    .cmd{color:#e5e7eb;}
    .out{color:#a7f3d0;}            /* minty output */
    .err{color:#fb7185;}            /* red-ish error */

    .termControls{
      display:flex;
      gap:8px;
      flex-wrap:wrap;
      padding:10px 12px;
      border-top:1px solid var(--border);
      background: rgba(255,255,255,.02);
    }
    .pill{
      border:1px solid var(--border);
      background: rgba(255,255,255,.03);
      color: var(--text);
      border-radius:999px;
      padding:8px 10px;
      font-size:12px;
      cursor:pointer;
      transition: background .15s ease;
      font-family: var(--mono);
    }
    .pill:hover{background: rgba(255,255,255,.06);}
    .pill:active{transform: scale(.99);}

    /* small screens */
    @media (max-width: 980px){
      body{overflow:auto;}
      .app{grid-template-columns:1fr; height:auto;}
    }

    /* Copilot bridge buttons */
    .copilotBtn{
      padding:4px 12px;
      border:1px solid rgba(34,197,94,.45);
      background: rgba(34,197,94,.10);
      color: rgba(230,238,252,.85);
      border-radius:999px;
      font-size:12px;
      cursor:pointer;
      font-family: var(--sans);
      transition: background .15s ease;
      white-space:nowrap;
    }
    .copilotBtn:hover{background: rgba(34,197,94,.22);}
    .copilotBtn:active{transform: scale(.98);}

    .repoSelect{
      appearance:none;
      -webkit-appearance:none;
      background:rgba(0,0,0,.22);
      border:1px solid var(--border);
      border-radius:999px;
      color:var(--text);
      font-family:var(--sans);
      font-size:11px;
      padding:4px 8px;
      cursor:pointer;
      outline:none;
      transition:background .15s;
    }
    .repoSelect:hover{background:rgba(255,255,255,.05);}
    .repoSelect option{background:#0b0f14; color:var(--text);}

    .repoBadge{
      font-size:11px;
      color:rgba(34,197,94,.85);
      user-select:none;
      white-space:nowrap;
    }

    .copilotMsgBtn{
      display:inline-block;
      margin-top:7px;
      padding:3px 10px;
      border:1px solid rgba(34,197,94,.30);
      background: rgba(34,197,94,.07);
      color: rgba(230,238,252,.65);
      border-radius:999px;
      font-size:11px;
      cursor:pointer;
      font-family: var(--sans);
      transition: background .15s ease;
    }
    .copilotMsgBtn:hover{background: rgba(34,197,94,.18); color: rgba(230,238,252,.90);}
    .copilotMsgBtn:active{transform: scale(.98);}

    /* Agent tabs (segmented control) */
    .agentTabs{
      display:flex;
      gap:3px;
      background: rgba(0,0,0,.20);
      border:1px solid var(--border);
      border-radius:999px;
      padding:3px;
    }
    .tabBtn{
      padding:4px 12px;
      border-radius:999px;
      border:none;
      background:transparent;
      color:var(--muted);
      font-size:12px;
      font-weight:600;
      cursor:pointer;
      transition:background .15s,color .15s;
      font-family:var(--sans);
      white-space:nowrap;
    }
    .tabBtn.active{
      background:linear-gradient(180deg,rgba(34,197,94,.95),rgba(22,163,74,.95));
      color:#fff;
    }
    .tabBtn:hover:not(.active){
      background:rgba(255,255,255,.06);
      color:var(--text);
    }

    /* Team review buttons bar */
    .teamBtnsBar{
      display:flex;
      gap:6px;
      padding:7px 12px;
      border-bottom:1px solid var(--border);
      background:rgba(255,255,255,.01);
      flex-wrap:wrap;
      align-items:center;
    }
    .teamBtn{
      padding:4px 12px;
      border-radius:999px;
      border:1px solid rgba(255,255,255,.12);
      background:rgba(255,255,255,.04);
      color:var(--text);
      font-size:12px;
      cursor:pointer;
      font-family:var(--sans);
      transition:background .15s;
      white-space:nowrap;
    }
    .teamBtn:hover{background:rgba(255,255,255,.09);}
    .teamBtn:active{transform:scale(.98);}
    .teamBtn:disabled{opacity:.45;cursor:default;pointer-events:none;}
    .cancelBtn{
      border-color:rgba(251,113,133,.35);
      color:rgba(251,113,133,.85);
      background:rgba(251,113,133,.06);
    }
    .cancelBtn:hover{background:rgba(251,113,133,.14);}

    /* Team activity feed */
    .teamFeed{
      flex:1;
      overflow:auto;
      padding:10px 12px;
      display:none;
      flex-direction:column;
      gap:5px;
      scroll-behavior:smooth;
    }
    .teamFeed::-webkit-scrollbar{width:10px;}
    .teamFeed::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);border-radius:999px;}
    .feedRow{display:flex;flex-direction:column;gap:3px;}
    .feedMeta{
      display:flex;
      gap:6px;
      align-items:center;
      font-size:11px;
      color:var(--muted);
      user-select:none;
    }
    .feedAgent{
      padding:2px 7px;
      border-radius:999px;
      font-size:11px;
      font-weight:600;
      letter-spacing:.2px;
    }
    .feedAgent.pnl   {background:rgba(59,130,246,.18);color:#93c5fd;}
    .feedAgent.quant {background:rgba(167,139,250,.18);color:#c4b5fd;}
    .feedAgent.coo   {background:rgba(34,197,94,.18);color:#86efac;}
    .feedAgent.main  {background:rgba(251,146,60,.18);color:rgba(251,146,60,.9);}
    .feedAgent.vibe  {background:rgba(45,212,191,.18);color:rgba(45,212,191,.9);}
    .feedAgent.system{background:rgba(156,163,175,.15);color:#9ca3af;}
    .feedType{
      font-size:10px;
      padding:1px 6px;
      border-radius:999px;
      border:1px solid var(--border);
      color:var(--muted);
    }
    .feedType.run-start,.feedType.start{border-color:rgba(59,130,246,.3);color:#93c5fd;}
    .feedType.done   {border-color:rgba(34,197,94,.3);color:#86efac;}
    .feedType.error,.feedType.cancelled{border-color:rgba(251,113,133,.3);color:#fca5a5;}
    .feedType.action {border-color:rgba(250,204,21,.4);color:#fde047;}
    .feedContent{
      font-size:13px;
      color:var(--text);
      white-space:pre-wrap;
      line-height:1.4;
      padding:8px 10px;
      background:rgba(0,0,0,.18);
      border:1px solid var(--border);
      border-radius:10px;
    }
    .feedContent.feedAction{
      background:rgba(250,204,21,.06);
      border-color:rgba(250,204,21,.25);
    }
    .feedContent.feedAction a{color:#fde047;text-decoration:underline;}
    .feedType.proposal{border-color:rgba(99,202,183,.4);color:#6ecfc3;}
    .feedType.action_pending{border-color:rgba(251,191,36,.4);color:rgba(251,191,36,.9);}
    .feedType.no-action{border-color:rgba(156,163,175,.3);color:#9ca3af;}

    /* Proposal confirmation cards */
    .proposalCard{
      margin-top:4px;
      padding:10px 12px;
      background:rgba(99,202,183,.06);
      border:1px solid rgba(99,202,183,.25);
      border-radius:10px;
      display:flex;
      flex-direction:column;
      gap:8px;
    }
    .proposalTitle{
      font-size:13px;
      font-weight:600;
      color:var(--text);
      line-height:1.4;
    }
    .proposalActions{
      display:flex;
      flex-wrap:wrap;
      gap:6px;
      align-items:center;
    }
    .proposalRepoSelect{
      background:rgba(255,255,255,.06);
      border:1px solid rgba(255,255,255,.15);
      color:var(--text);
      border-radius:6px;
      padding:3px 7px;
      font-size:12px;
      font-family:var(--sans);
      cursor:pointer;
    }
    .proposalConfirmBtn{
      padding:4px 13px;
      border-radius:999px;
      border:1px solid rgba(34,197,94,.45);
      background:rgba(34,197,94,.12);
      color:#86efac;
      font-size:12px;
      cursor:pointer;
      font-family:var(--sans);
      transition:background .15s;
    }
    .proposalConfirmBtn:hover:not(:disabled){background:rgba(34,197,94,.22);}
    .proposalConfirmBtn:disabled{opacity:.45;cursor:default;}
    .proposalDismissBtn{
      padding:4px 11px;
      border-radius:999px;
      border:1px solid rgba(255,255,255,.12);
      background:rgba(255,255,255,.04);
      color:var(--muted);
      font-size:12px;
      cursor:pointer;
      font-family:var(--sans);
      transition:background .15s;
    }
    .proposalDismissBtn:hover:not(:disabled){background:rgba(255,255,255,.09);}
    .proposalDismissBtn:disabled{opacity:.45;cursor:default;}
    .proposalResult{
      font-size:12px;
      color:var(--muted);
      line-height:1.4;
    }
    .proposalResult a{color:#86efac;text-decoration:underline;}

    /* Shared control button styles */
    .vibeLabel{
      font-size:11px;
      color:var(--muted);
      font-weight:600;
      letter-spacing:.3px;
      text-transform:uppercase;
      margin-bottom:2px;
      display:block;
    }
    .vibeTextarea{
      width:100%;
      box-sizing:border-box;
      background:rgba(0,0,0,.22);
      border:1px solid var(--border);
      border-radius:10px;
      padding:8px 10px;
      color:var(--text);
      font-family:var(--sans);
      font-size:13px;
      line-height:1.35;
      outline:none;
      resize:vertical;
      min-height:80px;
    }
    .vibeTextarea:focus{border-color:rgba(34,197,94,.4);}
    .vibeBtn{
      padding:6px 14px;
      border-radius:999px;
      font-size:12px;
      font-family:var(--sans);
      font-weight:600;
      cursor:pointer;
      transition:background .15s,filter .1s;
      white-space:nowrap;
    }
    .vibeBtn:active{transform:scale(.98);}
    .vibeBtn:disabled{opacity:.45;cursor:default;pointer-events:none;}
    .vibePrimaryBtn{
      border:1px solid rgba(34,197,94,.45);
      background:linear-gradient(180deg,rgba(34,197,94,.95),rgba(22,163,74,.95));
      color:#fff;
    }
    .vibePrimaryBtn:hover:not(:disabled){filter:brightness(1.08);}
    .vibeSecondaryBtn{
      border:1px solid var(--border);
      background:rgba(255,255,255,.05);
      color:var(--text);
    }
    .vibeSecondaryBtn:hover:not(:disabled){background:rgba(255,255,255,.10);}
    .vibeDangerBtn{
      border:1px solid rgba(251,113,133,.4);
      background:rgba(251,113,133,.07);
      color:rgba(251,113,133,.9);
    }
    .vibeDangerBtn:hover:not(:disabled){background:rgba(251,113,133,.15);}
    .vibeApprovalBanner{
      margin:10px 12px 0;
      padding:10px 12px;
      border:1px solid rgba(251,191,36,.35);
      background:rgba(251,191,36,.06);
      border-radius:12px;
      display:none;
      flex-direction:column;
      gap:8px;
    }
    .vibeApprovalTitle{
      font-size:12px;
      font-weight:700;
      color:rgba(251,191,36,.90);
    }
    .vibeApprovalCmd{
      font-family:var(--mono);
      font-size:12px;
      color:var(--text);
      background:rgba(0,0,0,.25);
      border-radius:8px;
      padding:8px 10px;
      white-space:pre-wrap;
      word-break:break-all;
    }
    .vibeApprovalBtns{display:flex;gap:8px;}

    /* Analytics tab */
    .analyticsPad{
      flex:1;
      display:none;
      flex-direction:column;
      overflow:hidden;
    }
    .analyticsToolbar{
      padding:8px 12px;
      border-bottom:1px solid var(--border);
      background:rgba(255,255,255,.01);
      display:flex;
      align-items:center;
      gap:8px;
      flex-wrap:wrap;
    }
    .analyticsBody{
      flex:1;
      overflow:auto;
      padding:12px;
      display:flex;
      flex-direction:column;
      gap:10px;
    }
    .analyticsBody::-webkit-scrollbar{width:10px;}
    .analyticsBody::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);border-radius:999px;}
    .analyticsSection{
      background:rgba(0,0,0,.18);
      border:1px solid var(--border);
      border-radius:12px;
      padding:12px 14px;
    }
    .analyticsSectionTitle{
      font-size:11px;
      font-weight:700;
      text-transform:uppercase;
      letter-spacing:.6px;
      color:var(--muted);
      margin-bottom:8px;
    }
    .analyticsRaw{
      font-family:var(--mono);
      font-size:12px;
      white-space:pre-wrap;
      word-break:break-all;
      color:var(--text);
      line-height:1.5;
    }
    .analyticsEmpty{
      color:var(--muted);
      font-size:13px;
      text-align:center;
      padding:28px 0;
    }

    /* Main tab quick-action suggestion chips */
    .mainSuggestsBar{
      display:none;
      gap:6px;
      padding:7px 12px;
      border-bottom:1px solid var(--border);
      background:rgba(255,255,255,.01);
      flex-wrap:wrap;
      align-items:center;
      font-size:12px;
      color:var(--muted);
    }
    .suggestChip{
      padding:4px 11px;
      border-radius:999px;
      border:1px solid rgba(34,197,94,.25);
      background:rgba(34,197,94,.06);
      color:rgba(230,238,252,.75);
      font-size:12px;
      cursor:pointer;
      font-family:var(--sans);
      transition:background .15s,color .15s,border-color .15s;
      white-space:nowrap;
    }
    .suggestChip:hover{
      background:rgba(34,197,94,.15);
      color:var(--text);
      border-color:rgba(34,197,94,.45);
    }
    .suggestChip:active{transform:scale(.98);}

    /* Cheap Chat provider bar */
    .cheapBar{
      display:none;
      gap:6px;
      padding:7px 12px;
      border-bottom:1px solid var(--border);
      background:rgba(255,255,255,.01);
      flex-wrap:wrap;
      align-items:center;
      font-size:12px;
      color:var(--muted);
    }
    .cheapProviderBtn{
      padding:3px 12px;
      border-radius:999px;
      border:1px solid var(--border);
      background:rgba(255,255,255,.04);
      color:var(--muted);
      font-size:12px;
      font-weight:600;
      cursor:pointer;
      transition:background .15s,color .15s;
      font-family:var(--sans);
      white-space:nowrap;
    }
    .cheapProviderBtn.active{
      background:linear-gradient(180deg,rgba(34,197,94,.95),rgba(22,163,74,.95));
      color:#fff;
      border-color:transparent;
    }
    .cheapProviderBtn:hover:not(.active){background:rgba(255,255,255,.08);color:var(--text);}
    .cheapModelLabel{
      font-size:11px;
      color:rgba(34,197,94,.7);
      user-select:none;
    }
    .backendBanner{
      display:none;
      position:fixed;top:0;left:0;right:0;
      z-index:9999;
      background:rgba(251,113,133,.15);
      border-bottom:1px solid rgba(251,113,133,.4);
      color:#fca5a5;
      font-size:13px;
      padding:10px 16px;
      text-align:center;
    }

    /* Memory cockpit */
    .memoryPad{
      flex:1;
      display:none;
      flex-direction:column;
      overflow:hidden;
    }
    .memoryToolbar{
      padding:8px 12px;
      border-bottom:1px solid var(--border);
      background:rgba(255,255,255,.01);
      display:flex;
      align-items:center;
      gap:8px;
      flex-wrap:wrap;
    }
    .memoryAgentLabel{
      font-size:12px;
      color:var(--muted);
      user-select:none;
    }
    .memoryAgentTabs{display:flex;gap:4px;}
    .memoryAgentBtn{
      padding:3px 10px;
      border-radius:999px;
      border:1px solid var(--border);
      background:rgba(255,255,255,.04);
      color:var(--muted);
      font-size:12px;
      font-weight:600;
      cursor:pointer;
      transition:background .15s,color .15s;
      font-family:var(--sans);
    }
    .memoryAgentBtn.active{
      background:linear-gradient(180deg,rgba(34,197,94,.95),rgba(22,163,74,.95));
      color:#fff;
      border-color:transparent;
    }
    .memoryAgentBtn:hover:not(.active){background:rgba(255,255,255,.08);color:var(--text);}
    .memoryMeta{
      padding:6px 12px;
      font-size:11px;
      color:var(--muted);
      border-bottom:1px solid var(--border);
      background:rgba(0,0,0,.08);
      min-height:22px;
    }
    .memoryEditorSection{
      padding:10px 12px;
      border-bottom:1px solid var(--border);
      display:flex;
      flex-direction:column;
      gap:6px;
    }
    .memoryEditor{
      font-family:var(--mono) !important;
      font-size:12px !important;
      min-height:130px;
    }
    .memoryEventsSection{
      flex:1;
      overflow:auto;
      padding:10px 12px;
      display:flex;
      flex-direction:column;
      gap:4px;
    }
    .memoryEventsSection::-webkit-scrollbar{width:10px;}
    .memoryEventsSection::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);border-radius:999px;}
    .memEventRow{
      font-size:11px;
      color:var(--muted);
      padding:4px 8px;
      background:rgba(0,0,0,.12);
      border-radius:6px;
      border:1px solid var(--border);
      border-left:2px solid transparent;
      font-family:var(--mono);
      white-space:pre-wrap;
      word-break:break-all;
    }
    .memEventRow.update        {border-left-color:rgba(34,197,94,.55);}
    .memEventRow.invalidate    {border-left-color:rgba(251,113,133,.55);}
    .memEventRow.operator_update{border-left-color:rgba(251,191,36,.55);}

    /* Analytics stat cards */
    .statsRow{
      display:flex;
      gap:8px;
      flex-wrap:wrap;
    }
    .statCard{
      flex:1;
      min-width:90px;
      background:rgba(0,0,0,.28);
      border:1px solid var(--border);
      border-radius:10px;
      padding:10px 12px;
      display:flex;
      flex-direction:column;
      gap:4px;
    }
    .statCardLabel{
      font-size:10px;
      text-transform:uppercase;
      letter-spacing:.5px;
      color:var(--muted);
      font-weight:600;
    }
    .statCardValue{
      font-size:17px;
      font-weight:700;
      font-family:var(--mono);
      color:var(--text);
      line-height:1.2;
    }
    .statCardValue.pos{color:#86efac;}
    .statCardValue.neg{color:#fca5a5;}

    /* Analytics charts */
    .chartWrap{
      background:rgba(0,0,0,.22);
      border:1px solid var(--border);
      border-radius:12px;
      padding:12px 14px;
      overflow:hidden;
    }
    .chartTitle{
      font-size:11px;
      font-weight:700;
      text-transform:uppercase;
      letter-spacing:.6px;
      color:var(--muted);
      margin-bottom:8px;
    }
    canvas.analyticsCanvas{
      width:100%;
      height:150px;
      display:block;
    }

    /* Analytics trades table */
    .tradesTableWrap{overflow-x:auto;}
    table.tradesTable{
      width:100%;
      border-collapse:collapse;
      font-size:12px;
      font-family:var(--mono);
    }
    .tradesTable th{
      text-align:left;
      padding:6px 10px;
      border-bottom:1px solid var(--border);
      color:var(--muted);
      font-size:10px;
      text-transform:uppercase;
      letter-spacing:.4px;
      font-weight:700;
      white-space:nowrap;
    }
    .tradesTable td{
      padding:5px 10px;
      border-bottom:1px solid rgba(255,255,255,.04);
      color:var(--text);
      white-space:nowrap;
    }
    .tradesTable tr:hover td{background:rgba(255,255,255,.03);}
    .tradeSide.buy {color:#86efac;}
    .tradeSide.sell{color:#fca5a5;}
    .tradeTag.good   {color:#86efac;font-weight:700;}
    .tradeTag.bad    {color:#fca5a5;font-weight:700;}
    .tradeTag.neutral{color:#fbbf24;font-weight:700;}

    /* Live exchange panels */
    #krakenLiveWrap, #alpacaLiveWrap {
      background:rgba(0,0,0,.18);
      border:1px solid var(--border);
      border-radius:10px;
      padding:10px 12px;
      margin-bottom:10px;
    }
    .liveExchangeEmpty{
      font-size:12px;
      color:var(--muted);
      padding:8px 0;
    }
  </style>
</head>

<body>
<div id="backendBanner" class="backendBanner">
  ⚠️ Backend unreachable at <code id="backendBannerUrl"></code> — check that <code>uvicorn web_app:app --reload</code> is running.
</div>
  <div class="app">
    <!-- LEFT -->
    <section class="card" id="leftCard">
      <div class="cardHeader">
        <div class="title">
          <span id="repoSwatch" style="width:10px;height:10px;border-radius:999px;background:var(--accent);display:inline-block;transition:background .3s ease,box-shadow .3s ease"></span>
          Agents
          <span id="repoThemeLabel" style="font-size:11px;font-weight:500;color:var(--muted);opacity:.75;display:none"></span>
        </div>
        <div class="agentTabs">
          <button class="tabBtn active" data-agent="main">Main</button>
          <button class="tabBtn" data-agent="pnl">P&amp;L</button>
          <button class="tabBtn" data-agent="quant">Quant</button>
          <button class="tabBtn" data-agent="coo">COO</button>
          <button class="tabBtn" data-agent="team">Team</button>
          <button class="tabBtn" data-agent="analytics">📊 Analytics</button>
          <button class="tabBtn" data-agent="memory">🧠 Memory</button>
          <button class="tabBtn" data-agent="cheap">💬 Chat</button>
        </div>
        <div class="badge" id="statusBadge">ready</div>
      </div>

      <div class="teamBtnsBar" id="teamBtnsBar" style="display:none">
        <button class="teamBtn" id="quickReviewBtn">⚡ Quick team review</button>
        <button class="teamBtn" id="detailedReviewBtn">🔍 Detailed team review</button>
        <button class="teamBtn" id="yearlyReviewBtn">📅 Yearly review</button>
        <button class="teamBtn cancelBtn" id="cancelReviewBtn" style="display:none">✕ Cancel run</button>
      </div>

      <div class="cheapBar" id="cheapBar">
        <span>Provider:</span>
        <button class="cheapProviderBtn active" data-provider="groq">Groq</button>
        <button class="cheapProviderBtn" data-provider="mistral">Mistral</button>
        <button class="cheapProviderBtn" data-provider="cerebras">Cerebras</button>
        <span class="cheapModelLabel" id="cheapModelLabel">llama-3.3-70b-versatile</span>
      </div>

      <!-- Main tab quick-action suggestion chips -->
      <div class="mainSuggestsBar" id="mainSuggestsBar">
        <span>Ask:</span>
        <button class="suggestChip" data-suggest="What's the total P&L for crypto and Alpaca trades?">📊 Total P&amp;L</button>
        <button class="suggestChip" data-suggest="Show me the latest crypto trades from the bot.">🔄 Crypto trades</button>
        <button class="suggestChip" data-suggest="What is the Alpaca bot's current P&L and status?">📈 Alpaca P&amp;L</button>
        <button class="suggestChip" data-suggest="Check the system and container health — is the bot running?">🏥 System health</button>
        <button class="suggestChip" data-suggest="What are the recent git commits across my repos?">🗂 Git status</button>
        <button class="suggestChip" data-suggest="What are the latest crypto news and BTC price?">🌐 Crypto news</button>
        <button class="suggestChip" data-suggest="Is the bot halted? What triggered it?">🛑 Halt status</button>
      </div>

      <div id="chat-main" class="chatBody"></div>
      <div id="chat-pnl"  class="chatBody" style="display:none"></div>
      <div id="chat-quant" class="chatBody" style="display:none"></div>
      <div id="chat-coo"  class="chatBody" style="display:none"></div>
      <div id="chat-cheap" class="chatBody" style="display:none"></div>

      <div class="teamFeed" id="teamFeed"></div>

      <!-- Analytics tab panel -->
      <div class="analyticsPad" id="analyticsPad">
        <div class="analyticsToolbar">
          <button class="vibeBtn vibePrimaryBtn" id="analyticsLoadBtn">📈 Load Charts &amp; Table</button>
          <button class="vibeBtn vibeSecondaryBtn" id="analyticsFetchBtn">📡 SSH Probe</button>
          <button class="vibeBtn vibeSecondaryBtn" id="krakenPullBtn">🐙 Pull Kraken</button>
          <button class="vibeBtn vibeSecondaryBtn" id="alpacaPullBtn">🦙 Pull Alpaca</button>
          <span id="analyticsStatus" style="font-size:12px;color:var(--muted);"></span>
        </div>
        <!-- Live exchange panels (populated by JS) -->
        <div id="krakenLiveWrap" style="display:none">
          <div class="analyticsSectionTitle" style="margin-top:10px;">🐙 Kraken — Live Positions &amp; Balance</div>
          <div class="statsRow" id="krakenBalanceStats" style="margin-bottom:6px;"></div>
          <div class="tradesTableWrap" id="krakenPositionsTable"></div>
          <div class="analyticsSectionTitle" style="margin-top:10px;font-size:11px;">Recent Trade History</div>
          <div class="tradesTableWrap" id="krakenTradesTable"></div>
        </div>
        <div id="alpacaLiveWrap" style="display:none">
          <div class="analyticsSectionTitle" style="margin-top:10px;">🦙 Alpaca — Live Positions &amp; Account</div>
          <div class="statsRow" id="alpacaAccountStats" style="margin-bottom:6px;"></div>
          <div class="tradesTableWrap" id="alpacaPositionsTable"></div>
          <div class="analyticsSectionTitle" style="margin-top:10px;font-size:11px;">Recent Trade History</div>
          <div class="tradesTableWrap" id="alpacaTradesTable"></div>
        </div>
        <div class="analyticsBody" id="analyticsBody">
          <!-- KPI stat cards (populated by JS) -->
          <div class="statsRow" id="analyticsStats" style="display:none"></div>
          <!-- P&L chart -->
          <div class="chartWrap" id="pnlChartWrap" style="display:none">
            <div class="chartTitle">P&amp;L Over Time</div>
            <canvas class="analyticsCanvas" id="pnlChart"></canvas>
          </div>
          <!-- Equity chart -->
          <div class="chartWrap" id="equityChartWrap" style="display:none">
            <div class="chartTitle">Equity Over Time</div>
            <canvas class="analyticsCanvas" id="equityChart"></canvas>
          </div>
          <!-- Trade executions table (populated by JS) -->
          <div class="analyticsSection" id="tradesTableSection" style="display:none">
            <div class="analyticsSectionTitle">Recent Trade Executions</div>
            <div class="tradesTableWrap" id="tradesTableWrap"></div>
          </div>
          <!-- SSH raw sections injected dynamically -->
          <div class="analyticsEmpty" id="analyticsEmpty">
            Click <strong>Load Charts &amp; Table</strong> for visual analytics from the local trade log, <strong>SSH Probe</strong> for raw VPS data, or <strong>Pull Kraken / Pull Alpaca</strong> to fetch live positions &amp; P&amp;L from the exchange APIs.
          </div>
        </div>
      </div>

      <!-- Memory cockpit panel -->
      <div class="memoryPad" id="memoryPad">
        <div class="memoryToolbar">
          <span class="memoryAgentLabel">Agent:</span>
          <div class="memoryAgentTabs">
            <button class="memoryAgentBtn active" data-memory-agent="pnl">P&amp;L</button>
            <button class="memoryAgentBtn" data-memory-agent="quant">Quant</button>
            <button class="memoryAgentBtn" data-memory-agent="coo">COO</button>
          </div>
          <span style="flex:1"></span>
          <button class="vibeBtn vibeSecondaryBtn" id="memRefreshBtn" style="font-size:11px;padding:4px 10px;">🔄 Refresh</button>
          <button class="vibeBtn vibeDangerBtn" id="memInvalidateBtn" style="font-size:11px;padding:4px 10px;">🗑 Invalidate</button>
          <button class="vibeBtn vibePrimaryBtn" id="memSaveBtn" style="font-size:11px;padding:4px 10px;">💾 Save</button>
        </div>
        <div class="memoryMeta" id="memoryMeta">Select an agent above to load its memory snapshot.</div>
        <div class="memoryEditorSection">
          <span class="vibeLabel">Snapshot JSON — edit keys/values, then click Save</span>
          <textarea class="vibeTextarea memoryEditor" id="memoryEditor" rows="10" placeholder='{"key": "value"}'></textarea>
        </div>
        <div class="memoryEventsSection">
          <div class="analyticsSectionTitle" style="margin-bottom:6px;">Recent Memory Events</div>
          <div id="memoryEventsList"></div>
        </div>
      </div>

      <div class="chatComposer">
        <div class="composerRow">
          <div class="inputWrap">
            <button class="iconBtn" id="attachBtn" title="Attach file (text)">
              <!-- paperclip -->
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                <path d="M21 12.5l-7.6 7.6a5 5 0 0 1-7.1-7.1l8.5-8.5a3.5 3.5 0 0 1 5 5l-8.6 8.6a2 2 0 0 1-2.9-2.9l7.8-7.8" stroke="rgba(230,238,252,.75)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>

            <button class="iconBtn" id="imageBtn" title="Attach image (preview)">
              <!-- image icon -->
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                <path d="M21 19V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14" stroke="rgba(230,238,252,.75)" stroke-width="2" stroke-linecap="round"/>
                <path d="M3 17l6-6 4 4 3-3 5 5" stroke="rgba(230,238,252,.75)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                <circle cx="9" cy="8" r="1.5" fill="rgba(230,238,252,.75)"/>
              </svg>
            </button>

            <textarea id="input" placeholder="Message OpenClaw…" rows="1"></textarea>
          </div>

          <button class="sendBtn" id="sendBtn" title="Send">
            <!-- arrow -->
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
              <path d="M5 12h12" stroke="white" stroke-width="2" stroke-linecap="round"/>
              <path d="M13 6l6 6-6 6" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </button>
        </div>

        <div class="hintBar">
          <div>Enter = send • Shift+Enter = newline • <code>/copilot &lt;goal&gt;</code> • Team review: type <code>@filename</code> to attach a file</div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
            <select class="repoSelect" id="repoSelect" title="Target repo for Copilot issues">
              <option value="">Auto</option>
              <option value="leeheggan-droid/openclaw-crypto">openclaw-crypto</option>
              <option value="leeheggan-droid/alpaca_orb_bite_bot">alpaca_orb_bite_bot</option>
              <option value="leeheggan-droid/LinkedIn_Data_Centre_News">LinkedIn_Data_Centre_News</option>
              <option value="leeheggan-droid/openclaw-control">openclaw-control</option>
            </select>
            <span class="repoBadge" id="repoBadge"></span>
            <button class="copilotBtn" id="copilotBtn" title="Create a Copilot GitHub Issue from current context">🤖 Fix via Copilot</button>
          </div>
        </div>

        <input id="fileInput" type="file" hidden />
        <input id="imgInput" type="file" accept="image/*" hidden />
      </div>
    </section>

    <!-- RIGHT -->
    <section class="card">
      <div class="cardHeader">
        <div class="title">
          <span style="width:10px;height:10px;border-radius:999px;background:#60a5fa;display:inline-block"></span>
          Shell Output
          <span class="badge">ssh</span>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="font-size:12px;color:var(--muted);user-select:none;">SSH target:</span>
          <div class="badge" id="hostBadge">localhost</div>
          <span style="font-size:12px;color:var(--muted);user-select:none;margin-left:8px;">Domain:</span>
          <button class="pill" id="domainMainBtn" onclick="openDomain('main')" title="leeheggan.tech" style="padding:3px 10px;font-size:11px;">leeheggan.tech</button>
          <button class="pill" id="domainChatBtn" onclick="openDomain('webchat')" title="leeheggan.tech/web-chat" style="padding:3px 10px;font-size:11px;">web-chat</button>
        </div>
      </div>

      <div class="termBody" id="terminal"></div>

      <!-- READONLY approval banner — shown before each terminal pill command -->
      <div class="vibeApprovalBanner" id="readonlyApprovalBanner">
        <div class="vibeApprovalTitle">🔒 READONLY lane — confirm before running</div>
        <div class="vibeApprovalCmd" id="readonlyApprovalCmd"></div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px;" id="readonlyApprovalHost"></div>
        <div class="vibeApprovalBtns">
          <button class="vibeBtn vibePrimaryBtn" id="readonlyConfirmBtn">✅ Confirm &amp; Run</button>
          <button class="vibeBtn vibeDangerBtn" id="readonlyCancelBtn">✗ Cancel</button>
        </div>
      </div>

      <div class="termControls">
        <button class="pill" onclick="clearTerminal()">clear</button>
      </div>
    </section>
  </div>

  <!-- Team review pre-assessment modal -->
  <div id="teamReviewModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:300;align-items:center;justify-content:center;padding:16px;">
    <div style="background:#111827;border:1px solid rgba(255,255,255,.14);border-radius:14px;padding:26px 24px;max-width:480px;width:100%;">
      <div style="font-size:1rem;font-weight:700;margin-bottom:6px;color:var(--text);" id="trModalTitle">Team Assessment</div>
      <div style="font-size:.82rem;color:var(--muted);margin-bottom:14px;">The team (P&amp;L, Quant, COO) will analyse your current workspace context. Optionally add a specific focus or question below.</div>
      <label style="font-size:.82rem;color:var(--muted);display:block;margin-bottom:5px;">Additional context <span style="opacity:.6">(optional)</span></label>
      <textarea id="trModalContext" rows="4" style="width:100%;box-sizing:border-box;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.12);border-radius:8px;padding:9px 11px;color:var(--text);font-size:.88rem;font-family:inherit;resize:vertical;" placeholder="e.g. Focus on the crypto bot's win rate, or ask a specific question…"></textarea>
      <div style="display:flex;gap:8px;margin-top:16px;justify-content:flex-end;flex-wrap:wrap;">
        <button id="trModalCancelBtn" style="padding:8px 18px;border-radius:8px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.05);color:var(--text);font-size:.88rem;cursor:pointer;font-family:inherit;">Cancel</button>
        <button id="trModalStartBtn" style="padding:8px 18px;border-radius:8px;border:none;background:var(--accent);color:#0b0f14;font-size:.88rem;font-weight:700;cursor:pointer;font-family:inherit;">▶ Start Review</button>
      </div>
    </div>
  </div>

<script>
  // --- API base URL ---
  // Defaults to same-origin. Server injects window.OPENCLAW_GATEWAY_URL when the
  // OPENCLAW_GATEWAY_URL env-var is set; otherwise same-origin paths are used.
  const API_BASE = (typeof window.OPENCLAW_GATEWAY_URL !== "undefined" && window.OPENCLAW_GATEWAY_URL)
    ? window.OPENCLAW_GATEWAY_URL.replace(/[/]$/, "")
    : window.location.origin;

  // --- DOM references ---
  const CHAT_PANES = {
    main:  document.getElementById("chat-main"),
    pnl:   document.getElementById("chat-pnl"),
    quant: document.getElementById("chat-quant"),
    coo:   document.getElementById("chat-coo"),
    cheap: document.getElementById("chat-cheap"),
  };
  function activeChatPane() { return CHAT_PANES[activeAgent] || null; }
  const terminalEl = document.getElementById("terminal");
  const inputEl = document.getElementById("input");
  const sendBtn = document.getElementById("sendBtn");
  const statusBadge = document.getElementById("statusBadge");
  const hostBadge = document.getElementById("hostBadge");

  const fileInput = document.getElementById("fileInput");
  const imgInput = document.getElementById("imgInput");
  document.getElementById("attachBtn").onclick = () => fileInput.click();
  document.getElementById("imageBtn").onclick = () => imgInput.click();

  // --- multi-agent state ---
  const AGENT_LABELS = {main: "Main AI", pnl: "P&L", quant: "Quant", coo: "COO", cheap: "Cheap Chat"};
  const AGENT_STORE_KEYS = {
    main:  "openclaw_chat_main_v1",
    pnl:   "openclaw_chat_pnl_v1",
    quant: "openclaw_chat_quant_v1",
    coo:   "openclaw_chat_coo_v1",
    cheap: "openclaw_chat_cheap_v1",
  };

  let activeAgent = "main";
  const histories = {};
  for (const ag of Object.keys(AGENT_LABELS)) {
    try { histories[ag] = JSON.parse(localStorage.getItem(AGENT_STORE_KEYS[ag]) || "[]"); }
    catch { histories[ag] = []; }
  }

  // Migrate legacy single-agent history into "main"
  try {
    const legacy = JSON.parse(localStorage.getItem("openclaw_chat_history_v1") || "[]");
    if (legacy.length && !histories.main.length) {
      histories.main = legacy;
      saveHistory("main");
    }
  } catch {}

  function saveHistory(ag) {
    localStorage.setItem(AGENT_STORE_KEYS[ag], JSON.stringify((histories[ag] || []).slice(-100)));
  }

  /** Shell-quote a string: wrap in single-quotes, escaping any literal single-quotes. */
  function shellQuote(s) { return "'" + s.replace(/'/g, "'\\''") + "'"; }

  function scrollChatBottom() {
    const pane = activeChatPane();
    if (pane) pane.scrollTop = pane.scrollHeight;
  }
  function scrollTermBottom()  { terminalEl.scrollTop = terminalEl.scrollHeight; }

  function addChat(role, text, extraHTML) {
    const agLabel = role === "user" ? "You" : (AGENT_LABELS[activeAgent] || activeAgent);
    const row = document.createElement("div");
    row.className = "msgRow " + (role === "user" ? "user" : "agent");

    const wrap = document.createElement("div");

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = agLabel;
    wrap.appendChild(meta);

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text || "";
    if (extraHTML) {
      const holder = document.createElement("div");
      holder.innerHTML = extraHTML;
      bubble.appendChild(holder);
    }
    wrap.appendChild(bubble);

    // Add "Fix via Copilot" button on substantive agent messages
    if (role === "agent" && text && !/^[⏳✅🚀❌]/.test(text)) {
      const capturedText = text;
      const capturedAgent = activeAgent;
      const copBtn = document.createElement("button");
      copBtn.className = "copilotMsgBtn";
      copBtn.textContent = "🤖 Fix via Copilot";
      copBtn.onclick = () => {
        const hist = histories[capturedAgent] || [];
        const lastUser = [...hist].reverse().find(h => h.role === "user");
        const goal = prompt("Describe the goal for Copilot:", (lastUser ? lastUser.text : "").slice(0, 120)) || "";
        if (!goal) return;
        triggerCopilot(goal, lastUser ? lastUser.text : "", capturedText);
      };
      wrap.appendChild(copBtn);
    }

    row.appendChild(wrap);
    const pane = activeChatPane();
    if (pane) pane.appendChild(row);
    scrollChatBottom();
  }

  function _renderHistoryIntoPane(ag) {
    const pane = CHAT_PANES[ag];
    if (!pane) return;
    const savedAgent = activeAgent;
    activeAgent = ag;
    (histories[ag] || []).forEach(item => addChat(item.role, item.text, item.extraHTML || ""));
    activeAgent = savedAgent;
    pane.scrollTop = pane.scrollHeight;
  }
  // One-time initial render of all per-tab histories
  for (const ag of Object.keys(CHAT_PANES)) { _renderHistoryIntoPane(ag); }

  // --- tab switching ---
  const teamFeedEl = document.getElementById("teamFeed");
  const analyticsPadEl = document.getElementById("analyticsPad");
  const memoryPadEl    = document.getElementById("memoryPad");
  const cheapBarEl = document.getElementById("cheapBar");
  const mainSuggestsBarEl = document.getElementById("mainSuggestsBar");
  const composerEl = document.querySelector(".chatComposer");
  const teamBtnsBarEl = document.getElementById("teamBtnsBar");

  function showAgentTab(ag) {
    const isTeam = ag === "team";
    const isAnalytics = ag === "analytics";
    const isCheap = ag === "cheap";
    const isMemory = ag === "memory";
    const isMain = ag === "main";
    // Show the right chat pane (or none for team/analytics/memory) — no re-render
    for (const [key, pane] of Object.entries(CHAT_PANES)) {
      pane.style.display = (!isTeam && !isAnalytics && !isMemory && key === ag) ? "" : "none";
    }
    // Main tab gets its own suggest bar instead of the team review buttons
    teamBtnsBarEl.style.display  = (!isMain && !isAnalytics && !isMemory) ? "flex" : "none";
    mainSuggestsBarEl.style.display = isMain ? "flex" : "none";
    cheapBarEl.style.display     = isCheap     ? "flex" : "none";
    teamFeedEl.style.display     = isTeam      ? "flex" : "none";
    analyticsPadEl.style.display = isAnalytics ? "flex" : "none";
    memoryPadEl.style.display    = isMemory    ? "flex" : "none";
    composerEl.style.display     = (isTeam || isAnalytics || isMemory) ? "none" : "";
    if (isTeam) {
      renderTeamFeed();
    }
    if (isMemory) {
      loadMemoryCockpit();
    }
  }

  document.querySelectorAll(".tabBtn").forEach(btn => {
    btn.addEventListener("click", () => {
      const ag = btn.getAttribute("data-agent");
      if (ag === activeAgent) return;
      activeAgent = ag;
      document.querySelectorAll(".tabBtn").forEach(b => b.classList.toggle("active", b === btn));
      showAgentTab(ag);
    });
  });

  // --- Quick-action suggestion chips (Main tab) ---
  document.querySelectorAll(".suggestChip").forEach(chip => {
    chip.addEventListener("click", () => {
      const text = chip.getAttribute("data-suggest") || "";
      if (!text) return;
      inputEl.value = text;
      autoGrow();
      inputEl.focus();
    });
  });

  // Initialise main tab suggest bar visibility on page load (default tab is main)
  if (mainSuggestsBarEl) mainSuggestsBarEl.style.display = "flex";
  if (teamBtnsBarEl) teamBtnsBarEl.style.display = "none";

  // --- terminal helpers ---
  function termLine(kind, text) {
    const div = document.createElement("div");
    div.className = "termLine " + kind;
    div.textContent = text;
    terminalEl.appendChild(div);
    scrollTermBottom();
  }

  function clearTerminal() { terminalEl.innerHTML = ""; }

  function setBusy(isBusy) {
    statusBadge.textContent = isBusy ? "thinking…" : "ready";
    statusBadge.style.borderColor = isBusy ? "rgba(34,197,94,.55)" : "rgba(255,255,255,.08)";
    statusBadge.style.color      = isBusy ? "rgba(230,238,252,.85)" : "rgba(230,238,252,.65)";
  }

  function getShellOutput() {
    return Array.from(terminalEl.querySelectorAll(".termLine"))
      .map(el => el.textContent)
      .slice(-200)
      .join("\\n");
  }

  // Auto-grow textarea
  function autoGrow() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 130) + "px";
  }
  inputEl.addEventListener("input", autoGrow);
  autoGrow();

  // Send on Enter, newline on Shift+Enter
  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  sendBtn.onclick = () => send();

  // Attach file (text)
  fileInput.onchange = async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    if (["team", "analytics", "memory"].includes(activeAgent)) return;
    const maxBytes = 200 * 1024;
    if (f.size > maxBytes) {
      addChat("user", `(attached file too large for inline text: ${f.name}, ${f.size} bytes)`);
      histories[activeAgent].push({role:"user", text:`(attached file too large for inline text: ${f.name})`});
      saveHistory(activeAgent);
      fileInput.value = "";
      return;
    }
    const text = await f.text();
    const extra = `<div class="attachment">FILE: ${escapeHtml(f.name)}\\n\\n${escapeHtml(text)}</div>`;
    addChat("user", `Attached: ${f.name}`, extra);
    // Include file content in the text field (capped at 4000 chars) so it is
    // available when conversation history is built for team review context.
    const fileSnippet = text.slice(0, 4000);
    histories[activeAgent].push({role:"user", text:`Attached file: ${f.name}\n\n${fileSnippet}`, extraHTML: extra});
    saveHistory(activeAgent);
    fileInput.value = "";
  };

  // Attach image (preview)
  imgInput.onchange = async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    if (["team", "analytics", "memory"].includes(activeAgent)) return;
    const url = URL.createObjectURL(f);
    const extra = `<div class="imgPreview"><img src="${url}" alt="attachment"/></div>`;
    addChat("user", `Attached image: ${f.name}`, extra);
    histories[activeAgent].push({role:"user", text:`Attached image: ${f.name}`, extraHTML: extra});
    saveHistory(activeAgent);
    imgInput.value = "";
  };

  function escapeHtml(s) {
    return (s || "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
  }

  async function send(textOverride) {
    const text = (textOverride !== undefined) ? textOverride : inputEl.value.trim();
    if (!text) return;
    if (["team", "analytics", "memory"].includes(activeAgent)) return; // composer hidden on these tabs

    addChat("user", text);
    histories[activeAgent].push({role: "user", text});
    saveHistory(activeAgent);

    inputEl.value = "";
    autoGrow();

    // Cheap Chat tab — route to /cheap-chat
    if (activeAgent === "cheap") {
      setBusy(true);
      // Build history for the provider (last 20 turns, user+assistant only)
      const providerHistory = (histories.cheap || [])
        .slice(-21, -1)  // exclude the message we just pushed
        .filter(h => h.role === "user" || h.role === "agent")
        .map(h => ({role: h.role === "agent" ? "assistant" : "user", content: h.text}));
      try {
        const res = await fetch(API_BASE + "/cheap-chat", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            message: text,
            provider: cheapActiveProvider,
            history: providerHistory,
          }),
        });
        const data = await res.json();
        const out = data.reply || data.error || "(no response)";
        addChat("agent", out);
        histories.cheap.push({role: "agent", text: out});
        saveHistory("cheap");
      } catch(err) {
        const msg = "Web error: " + (err && err.message ? err.message : String(err));
        addChat("agent", msg);
        histories.cheap.push({role: "agent", text: msg});
        saveHistory("cheap");
      } finally {
        setBusy(false);
      }
      return;
    }

    // /copilot command
    const lc = text.toLowerCase();
    if (lc.startsWith("/copilot") || lc.startsWith("fix via copilot:")) {
      let goal;
      if (lc.startsWith("/copilot")) {
        goal = text.slice("/copilot".length).trim();
      } else {
        goal = text.slice("fix via copilot:".length).trim();
      }
      const hist = histories[activeAgent];
      const prevHistory = hist.slice(0, -1);
      const lastUser  = [...prevHistory].reverse().find(h => h.role === "user");
      const lastAgent = [...prevHistory].reverse().find(h => h.role === "agent");
      if (!goal) goal = prompt("Describe the goal for Copilot:", lastUser ? lastUser.text.slice(0, 120) : "") || "";
      if (goal) await triggerCopilot(goal, lastUser ? lastUser.text : "", lastAgent ? lastAgent.text : "");
      return;
    }

    setBusy(true);
    try {
      const res = await fetch(API_BASE + "/agent/message", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          agent: activeAgent,
          text,
          workspace: {
            terminal_tail: getShellOutput(),
            conversation_history: (histories[activeAgent] || []).slice(-30),
          },
        })
      });
      const data = await res.json();
      const out = data.output || data.error || "(no response)";
      addChat("agent", out);
      histories[activeAgent].push({role: "agent", text: out});
      saveHistory(activeAgent);
      // Auto-switch to Team tab when the backend escalated to a Team Review
      if (data.team_run_id) {
        activeTeamRunId = data.team_run_id;
        teamPollCursor = 0;
        teamRunCancelled = false;
        activeAgent = "team";
        document.querySelectorAll(".tabBtn").forEach(b =>
          b.classList.toggle("active", b.getAttribute("data-agent") === "team"));
        showAgentTab("team");
        setTeamRunning(true);
        await pollTeamReview();
        teamPollTimer = setInterval(pollTeamReview, 2000);
      }
    } catch(err) {
      const msg = "Web error: " + (err && err.message ? err.message : String(err));
      addChat("agent", msg);
      histories[activeAgent].push({role: "agent", text: msg});
      saveHistory(activeAgent);
    } finally {
      setBusy(false);
    }
  }

  // Fetch SSH host label from server config
  let serverRepoDir = "";
  let vibeReadonlyHost = "";
  fetch(API_BASE + "/config").then(r => r.json()).then(cfg => {
    if (cfg && cfg.ssh_host) hostBadge.textContent = cfg.ssh_host;
    if (cfg && cfg.ssh_readonly_host) vibeReadonlyHost = cfg.ssh_readonly_host;
    if (cfg && cfg.repo_dir) serverRepoDir = cfg.repo_dir;
    if (cfg && Array.isArray(cfg.allowed_repos) && cfg.allowed_repos.length) {
      ALLOWED_REPOS.length = 0;
      cfg.allowed_repos.forEach(r => ALLOWED_REPOS.push(r));
    }
    updateRepoBadge();
  }).catch(() => {
    const banner = document.getElementById("backendBanner");
    const urlEl  = document.getElementById("backendBannerUrl");
    if (banner && urlEl) { urlEl.textContent = API_BASE + "/config"; banner.style.display = "block"; }
  });

  // ── READONLY lane — terminal pill approval gate ───────────────────────────

  const readonlyApprovalBannerEl = document.getElementById("readonlyApprovalBanner");
  const readonlyApprovalCmdEl    = document.getElementById("readonlyApprovalCmd");
  const readonlyApprovalHostEl   = document.getElementById("readonlyApprovalHost");
  const readonlyConfirmBtnEl     = document.getElementById("readonlyConfirmBtn");
  const readonlyCancelBtnEl      = document.getElementById("readonlyCancelBtn");

  let _pendingReadonlyCmd = "";

  function showReadonlyApproval(cmd) {
    const host = vibeReadonlyHost || "<OPENCLAW_SSH_READONLY_HOST>";
    readonlyApprovalCmdEl.textContent = "ssh " + host + " " + shellQuote(cmd);
    readonlyApprovalHostEl.textContent = "Lane: READONLY  •  Host: " + host;
    _pendingReadonlyCmd = cmd;
    readonlyApprovalBannerEl.style.display = "flex";
    readonlyApprovalBannerEl.scrollIntoView({behavior: "smooth"});
  }

  function hideReadonlyApproval() {
    readonlyApprovalBannerEl.style.display = "none";
    _pendingReadonlyCmd = "";
  }

  readonlyCancelBtnEl.onclick = hideReadonlyApproval;

  readonlyConfirmBtnEl.onclick = async () => {
    const cmd = _pendingReadonlyCmd;
    if (!cmd) return;
    hideReadonlyApproval();
    if (!vibeReadonlyHost) {
      termLine("err", "READONLY SSH not configured (OPENCLAW_SSH_READONLY_HOST unset) — pill disabled.");
      return;
    }
    const host = vibeReadonlyHost;
    const prompt = host + ":$ " + cmd;
    termLine("prompt", prompt);
    setBusy(true);
    try {
      const res = await fetch(API_BASE + "/ops/ssh-readonly-run", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({cmd}),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({detail: res.statusText}));
        termLine("err", "READONLY SSH error: " + (detail.detail || res.statusText));
        return;
      }
      const data = await res.json();
      const out = (data.stdout || "");
      const err = (data.stderr || data.error || "");
      if (out) termLine("out", out.trimEnd());
      if (err) termLine("err", err.trimEnd());
      if (!out && !err) termLine("out", "[no output]");
    } catch(e) {
      termLine("err", "READONLY SSH request failed: " + (e && e.message ? e.message : String(e)));
    } finally {
      setBusy(false);
    }
  };

  /**
   * Run a read-only command via the READONLY SSH lane.
   * Shows an approval banner (command + host) and waits for operator confirmation.
   */
  function runReadonlyQuick(cmd) {
    showReadonlyApproval(cmd);
  }

  // --- Copilot bridge ---

  const ALLOWED_REPOS = [
    "leeheggan-droid/openclaw-crypto",
    "leeheggan-droid/alpaca_orb_bite_bot",
    "leeheggan-droid/LinkedIn_Data_Centre_News",
    "leeheggan-droid/openclaw-control",
  ];

  const repoSelectEl = document.getElementById("repoSelect");
  const repoBadgeEl  = document.getElementById("repoBadge");

  function autoDetectRepo() {
    const dir   = (serverRepoDir || "").toLowerCase();
    const shell = getShellOutput().toLowerCase();
    if (dir.includes("openclaw-crypto")) {
      return "leeheggan-droid/openclaw-crypto";
    }
    if (shell.includes("alpaca_orb_bite_bot")) {
      return "leeheggan-droid/alpaca_orb_bite_bot";
    }
    if (shell.includes("linkedin_data_centre_news") || shell.includes("linkedindatacentrenews")) {
      return "leeheggan-droid/LinkedIn_Data_Centre_News";
    }
    return "leeheggan-droid/openclaw-control";
  }

  function getTargetRepo() {
    const sel = repoSelectEl ? repoSelectEl.value : "";
    return sel && ALLOWED_REPOS.includes(sel) ? sel : autoDetectRepo();
  }

  // ── Repo colour themes ───────────────────────────────────────────────────────
  // Each repo gets a distinct hue applied as a subtle background tint on the
  // left chat card so the operator can instantly see which repo is in context.
  const REPO_THEMES = {
    "leeheggan-droid/openclaw-crypto":         { accent: "#22c55e", bg: "rgba(34,197,94,.055)",  border: "rgba(34,197,94,.30)",  label: "openclaw-crypto"         },
    "leeheggan-droid/alpaca_orb_bite_bot":     { accent: "#3b82f6", bg: "rgba(59,130,246,.06)",  border: "rgba(59,130,246,.30)",  label: "alpaca_orb_bite_bot"     },
    "leeheggan-droid/LinkedIn_Data_Centre_News":{ accent: "#f59e0b", bg: "rgba(245,158,11,.055)", border: "rgba(245,158,11,.30)", label: "LinkedIn_Data_Centre_News"},
    "leeheggan-droid/openclaw-control":        { accent: "#a855f7", bg: "rgba(168,85,247,.055)", border: "rgba(168,85,247,.30)", label: "openclaw-control"        },
  };
  const _DEFAULT_REPO_THEME = { accent: "#a855f7", bg: "rgba(168,85,247,.055)", border: "rgba(168,85,247,.30)", label: "" };

  const leftCardEl      = document.getElementById("leftCard");
  const repoSwatchEl    = document.getElementById("repoSwatch");
  const repoThemeLabelEl= document.getElementById("repoThemeLabel");

  function applyRepoTheme(repo) {
    const theme = REPO_THEMES[repo] || _DEFAULT_REPO_THEME;
    // Tint the left card border
    if (leftCardEl) {
      leftCardEl.style.borderColor = theme.border;
      leftCardEl.style.boxShadow   = `0 10px 30px rgba(0,0,0,.35), 0 0 0 1px ${theme.border}`;
    }
    // Tint all chat pane backgrounds
    for (const pane of Object.values(CHAT_PANES)) {
      if (pane) pane.style.background = theme.bg;
    }
    // Update the swatch dot colour
    if (repoSwatchEl) {
      repoSwatchEl.style.background  = theme.accent;
      repoSwatchEl.style.boxShadow   = `0 0 6px ${theme.accent}`;
    }
    // Show the repo label next to "Agents"
    if (repoThemeLabelEl) {
      repoThemeLabelEl.textContent   = theme.label;
      repoThemeLabelEl.style.color   = theme.accent;
      repoThemeLabelEl.style.display = "";
    }
  }

  function updateRepoBadge() {
    if (!repoBadgeEl) return;
    const sel = repoSelectEl ? repoSelectEl.value : "";
    if (sel && ALLOWED_REPOS.includes(sel)) {
      repoBadgeEl.textContent = "";
      repoBadgeEl.style.color = (REPO_THEMES[sel] || {}).accent || "";
      applyRepoTheme(sel);
    } else {
      const detected = autoDetectRepo();
      const short = detected.split("/")[1] || detected;
      repoBadgeEl.textContent = "→ " + short;
      repoBadgeEl.style.color = (REPO_THEMES[detected] || {}).accent || "";
      applyRepoTheme(detected);
    }
  }

  if (repoSelectEl) {
    repoSelectEl.addEventListener("change", updateRepoBadge);
  }

  async function triggerCopilot(goal, lastUserMsg, lastAgentMsg) {
    if (!goal) return;
    const shellOutput = getShellOutput();
    const targetRepo  = getTargetRepo();
    const repoShort   = targetRepo.split("/")[1] || targetRepo;

    const statusMsg = "⏳ Creating Copilot issue in " + repoShort + "…";
    addChat("agent", statusMsg);
    histories[activeAgent].push({role: "agent", text: statusMsg});
    saveHistory(activeAgent);

    let data;
    try {
      const res = await fetch(API_BASE + "/copilot", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          goal,
          last_user_msg: lastUserMsg || "",
          last_agent_response: lastAgentMsg || "",
          shell_output: shellOutput,
          target_repo: targetRepo,
        })
      });
      data = await res.json();
    } catch(err) {
      const msg = "❌ Copilot error: " + (err && err.message ? err.message : String(err));
      addChat("agent", msg);
      histories[activeAgent].push({role: "agent", text: msg});
      saveHistory(activeAgent);
      return;
    }

    if (data.error) {
      const msg = "❌ Copilot issue failed: " + data.error;
      addChat("agent", msg);
      histories[activeAgent].push({role: "agent", text: msg});
      saveHistory(activeAgent);
      return;
    }

    const issueUrl  = data.issue_url;
    const issueNum  = data.issue_number;
    const usedRepo  = data.used_repo || targetRepo;
    const usedShort = usedRepo.split("/")[1] || usedRepo;
    let msg = "✅ Copilot issue #" + issueNum + " created in " + usedShort + ":\\n" + issueUrl;
    if (data.assignment === "manual_required") {
      msg += "\\n\\n⚠️ Issue created. Assign Copilot manually in GitHub (Assignees → Copilot).";
    } else {
      msg += "\\n\\nMonitoring for PR…";
    }
    addChat("agent", msg);
    histories[activeAgent].push({role: "agent", text: msg});
    saveHistory(activeAgent);

    pollForPR(issueNum, usedRepo);
  }

  function pollForPR(issueNumber, repo) {
    const repoParam = repo ? encodeURIComponent(repo) : "";
    let attempts = 0;
    const maxAttempts = 20; // 20 × 15s = 5 min
    const timer = setInterval(async () => {
      attempts++;
      if (attempts > maxAttempts) { clearInterval(timer); return; }
      try {
        const url = API_BASE + "/copilot/poll/" + issueNumber + (repoParam ? "?repo=" + repoParam : "");
        const res = await fetch(url);
        const data = await res.json();
        if (data.pr_url) {
          clearInterval(timer);
          const msg = "🚀 Copilot PR created:\\n" + data.pr_url;
          addChat("agent", msg);
          histories[activeAgent].push({role: "agent", text: msg});
          saveHistory(activeAgent);
        }
      } catch {}
    }, 15000);
  }

  document.getElementById("copilotBtn").onclick = async () => {
    const hist = histories[activeAgent] || [];
    const lastUser  = [...hist].reverse().find(h => h.role === "user");
    const lastAgent = [...hist].reverse().find(h => h.role === "agent");
    const def = lastUser ? lastUser.text.slice(0, 120) : "";
    const goal = prompt("Describe the goal for Copilot:", def) || "";
    if (!goal) return;
    await triggerCopilot(goal, lastUser ? lastUser.text : "", lastAgent ? lastAgent.text : "");
  };

  // ── Domain switcher ───────────────────────────────────────────────────────

  const _DOMAINS = {
    main:    "https://leeheggan.tech",
    webchat: "https://leeheggan.tech/web-chat",
  };
  const _domainBtns = {
    main:    document.getElementById("domainMainBtn"),
    webchat: document.getElementById("domainChatBtn"),
  };

  function openDomain(key) {
    const url = _DOMAINS[key];
    if (!url) return;
    // Update active styling
    for (const [k, btn] of Object.entries(_domainBtns)) {
      if (btn) btn.style.outline = (k === key) ? "1px solid rgba(34,197,94,.7)" : "";
    }
    // Open the domain in a new tab
    window.open(url, "_blank", "noopener,noreferrer");
  }

  // ── Cheap Chat ────────────────────────────────────────────────────────────

  const _CHEAP_PROVIDER_MODELS = {
    groq:      "llama-3.3-70b-versatile",
    mistral:   "mistral-small-latest",
    cerebras:  "llama3.1-70b",
  };

  let cheapActiveProvider = "groq";
  const cheapModelLabelEl = document.getElementById("cheapModelLabel");

  document.querySelectorAll(".cheapProviderBtn").forEach(btn => {
    btn.addEventListener("click", () => {
      cheapActiveProvider = btn.getAttribute("data-provider");
      document.querySelectorAll(".cheapProviderBtn").forEach(b =>
        b.classList.toggle("active", b === btn));
      if (cheapModelLabelEl) {
        cheapModelLabelEl.textContent = _CHEAP_PROVIDER_MODELS[cheapActiveProvider] || "";
      }
    });
  });

  // ── Team review ──────────────────────────────────────────────────────────────

  const TEAM_FEED_KEY = "openclaw_team_feed_v1";
  const AGENT_DISPLAY = {main: "Main", pnl: "P&L", quant: "Quant", coo: "COO", system: "System"};

  const quickReviewBtn    = document.getElementById("quickReviewBtn");
  const detailedReviewBtn = document.getElementById("detailedReviewBtn");
  const yearlyReviewBtn   = document.getElementById("yearlyReviewBtn");
  const cancelReviewBtn   = document.getElementById("cancelReviewBtn");

  let teamFeedEvents = [];
  let activeTeamRunId = null;
  let teamPollCursor = 0;
  let teamPollTimer = null;
  let teamRunCancelled = false;

  /**
   * Resolve @filename tokens in a prompt string via the browser file picker.
   * Each @token triggers a single file-open dialog; the selected file's text
   * replaces the token.  Returns the resolved string, or null if the user
   * cancels without selecting a file.
   *
   * Example: "Review this: @system_logs.txt" → "Review this: [FILE:system_logs.txt]\n<content>"
   */
  async function resolveAtRefs(text) {
    const atPattern = /@([A-Za-z0-9_.\\-]+)/g;
    let result = text;
    let match;
    // Collect unique tokens first so we show one picker per unique @ref
    const tokens = [];
    while ((match = atPattern.exec(text)) !== null) {
      if (!tokens.includes(match[0])) tokens.push(match[0]);
    }
    for (const token of tokens) {
      const fname = token.slice(1); // strip leading @
      const file = await new Promise(resolve => {
        const inp = document.createElement("input");
        inp.type = "file";
        inp.onchange = ev => resolve(ev.target.files && ev.target.files[0] || null);
        inp.click();
      });
      if (!file) return null; // user cancelled
      const content = await file.text();
      const snippet = content.slice(0, 8000);
      result = result.split(token).join(`[FILE:${file.name}]\n${snippet}`);
    }
    return result;
  }

  try {
    teamFeedEvents = JSON.parse(localStorage.getItem(TEAM_FEED_KEY) || "[]");
  } catch { teamFeedEvents = []; }

  function saveTeamFeed() {
    localStorage.setItem(TEAM_FEED_KEY, JSON.stringify(teamFeedEvents.slice(-200)));
  }

  function _nowIso() {
    return new Date().toISOString().split(".")[0] + "Z";
  }

  function createFeedRow(ev) {
    const row = document.createElement("div");
    row.className = "feedRow";

    const meta = document.createElement("div");
    meta.className = "feedMeta";

    const ts = document.createElement("span");
    ts.textContent = (ev.t || "").replace("T", " ").replace("+00:00", "").replace("Z", "") + " UTC";
    meta.appendChild(ts);

    const agBadge = document.createElement("span");
    agBadge.className = "feedAgent " + (ev.agent || "system");
    agBadge.textContent = AGENT_DISPLAY[ev.agent] || ev.agent || "—";
    meta.appendChild(agBadge);

    const typeBadge = document.createElement("span");
    typeBadge.className = "feedType " + (ev.type || "");
    typeBadge.textContent = ev.type || "";
    meta.appendChild(typeBadge);

    row.appendChild(meta);

    // Show content for message/error/run-start/cancelled/action/action_pending/no-action events
    const showContent = ev.content && ["message","error","run-start","cancelled","action","action_pending","no-action"].includes(ev.type);
    if (showContent) {
      const content = document.createElement("div");
      content.className = "feedContent" + (ev.type === "action" ? " feedAction" : "");
      if (ev.type === "action") {
        // Render URLs in action events as clickable hyperlinks
        const urlRe = /https?:\\/\\/[^\\s<>"]+?(?=[.,;:!?)\\]]*(?:\\s|$))/g;
        let lastIdx = 0, m;
        while ((m = urlRe.exec(ev.content)) !== null) {
          if (m.index > lastIdx) {
            content.appendChild(document.createTextNode(ev.content.slice(lastIdx, m.index)));
          }
          const a = document.createElement("a");
          a.href = m[0]; a.target = "_blank"; a.rel = "noopener noreferrer";
          a.textContent = m[0];
          content.appendChild(a);
          lastIdx = urlRe.lastIndex;
        }
        if (lastIdx < ev.content.length) {
          content.appendChild(document.createTextNode(ev.content.slice(lastIdx)));
        }
      } else {
        content.textContent = ev.content;
      }
      row.appendChild(content);
    }

    // Proposal confirmation card — operator confirms or cancels each item
    if (ev.type === "proposal") {
      const card = document.createElement("div");
      card.className = "proposalCard";

      const titleEl = document.createElement("div");
      titleEl.className = "proposalTitle";
      titleEl.textContent = "📋 " + (ev.title || ev.content || "");
      card.appendChild(titleEl);

      const actionsEl = document.createElement("div");
      actionsEl.className = "proposalActions";

      // Repo selector: shown when the target repo is ambiguous
      let repoSelect = null;
      if (ev.repo_ambiguous && Array.isArray(ev.allowed_repos) && ev.allowed_repos.length > 0) {
        repoSelect = document.createElement("select");
        repoSelect.className = "proposalRepoSelect";
        ev.allowed_repos.forEach(r => {
          const opt = document.createElement("option");
          opt.value = r;
          opt.textContent = r;
          repoSelect.appendChild(opt);
        });
        actionsEl.appendChild(repoSelect);
      }

      const confirmBtn = document.createElement("button");
      confirmBtn.className = "proposalConfirmBtn";
      confirmBtn.textContent = "✅ Create Issue";

      const dismissBtn = document.createElement("button");
      dismissBtn.className = "proposalDismissBtn";
      dismissBtn.textContent = "✕ Dismiss";

      const resultEl = document.createElement("div");
      resultEl.className = "proposalResult";

      confirmBtn.onclick = () => _confirmProposal(ev, repoSelect, confirmBtn, dismissBtn, resultEl);
      dismissBtn.onclick = () => {
        card.style.opacity = "0.4";
        confirmBtn.disabled = true;
        dismissBtn.disabled = true;
        resultEl.textContent = "Dismissed.";
      };

      actionsEl.appendChild(confirmBtn);
      actionsEl.appendChild(dismissBtn);
      card.appendChild(actionsEl);
      card.appendChild(resultEl);
      row.appendChild(card);
    }

    return row;
  }

  async function _confirmProposal(ev, repoSelect, confirmBtn, dismissBtn, resultEl) {
    const repo = repoSelect ? repoSelect.value : (ev.repo || "");
    if (!repo) {
      resultEl.textContent = "❌ No repo selected.";
      return;
    }
    confirmBtn.disabled = true;
    dismissBtn.disabled = true;
    confirmBtn.textContent = "⏳ Creating…";
    resultEl.textContent = "";
    try {
      const res = await fetch(API_BASE + "/team/proposal/confirm", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          title: ev.title || ev.content || "",
          body: ev.body || "",
          repo: repo,
          labels: ["copilot", "team-review"],
        }),
      });
      const data = await res.json();
      if (data.error) {
        resultEl.textContent = "❌ " + data.error;
        confirmBtn.disabled = false;
        confirmBtn.textContent = "✅ Create Issue";
        dismissBtn.disabled = false;
      } else {
        confirmBtn.textContent = "✅ Created";
        const link = document.createElement("a");
        link.href = data.issue_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = data.issue_url;
        resultEl.textContent = "✅ Issue #" + data.issue_number + " created in " + (data.used_repo || repo) + ": ";
        resultEl.appendChild(link);
      }
    } catch(err) {
      resultEl.textContent = "❌ " + (err.message || String(err));
      confirmBtn.disabled = false;
      confirmBtn.textContent = "✅ Create Issue";
      dismissBtn.disabled = false;
    }
  }

  function renderTeamFeed() {
    teamFeedEl.innerHTML = "";
    teamFeedEvents.forEach(ev => teamFeedEl.appendChild(createFeedRow(ev)));
    teamFeedEl.scrollTop = teamFeedEl.scrollHeight;
  }

  function appendTeamEvent(ev) {
    teamFeedEvents.push(ev);
    saveTeamFeed();
    if (activeAgent === "team") {
      teamFeedEl.appendChild(createFeedRow(ev));
      teamFeedEl.scrollTop = teamFeedEl.scrollHeight;
    }
    // Inject agent message into its per-agent tab history
    if (ev.type === "message" && ev.agent && ev.agent !== "system") {
      const label = AGENT_DISPLAY[ev.agent] || ev.agent;
      histories[ev.agent] = histories[ev.agent] || [];
      histories[ev.agent].push({role: "agent", text: "[Team Review]\\n" + ev.content});
      saveHistory(ev.agent);
    }
  }

  function setTeamRunning(running) {
    quickReviewBtn.disabled    = running;
    detailedReviewBtn.disabled = running;
    yearlyReviewBtn.disabled   = running;
    cancelReviewBtn.style.display = running ? "" : "none";
    setBusy(running);
  }

  // reviewPeriod: optional string (e.g. "2-year"). Pass "" for no period-specific review.
  // extraContext: optional string from the pre-assessment modal.
  async function runTeamReview(mode, reviewPeriod, extraContext) {
    if (activeTeamRunId) return; // already running

    teamRunCancelled = false;
    activeTeamRunId = null;
    teamPollCursor = 0;

    const period = reviewPeriod || "";

    // Capture conversation history from the current tab (not from the Team tab itself)
    const sourceAgent = (activeAgent === "team") ? "main" : activeAgent;
    const convHistory = (histories[sourceAgent] || [])
      .slice(-30)
      .filter(h => h.role === "user" || h.role === "agent")
      .map(h => ({role: h.role, text: (h.text || "").slice(0, 2000)}));

    // Determine prompt: typed text in input > extra context from modal > last user message in history
    let userPrompt = inputEl.value.trim();
    if (!userPrompt && extraContext) {
      userPrompt = extraContext;
    } else if (userPrompt && extraContext) {
      userPrompt = userPrompt + "\\n\\nAdditional context:\\n" + extraContext;
    }
    // Else: leave userPrompt empty; backend will extract from conversation history

    // Resolve @filename references (e.g. @system_logs.txt) by opening a file
    // picker for each token and substituting the file's text content inline.
    if (userPrompt.includes("@")) {
      const resolved = await resolveAtRefs(userPrompt);
      if (resolved === null) return; // user cancelled a file picker
      userPrompt = resolved;
    }

    const defaultPrompt = period
      ? `Produce a ${period} periodic review: (1) P&L summary with halt-state impact, ` +
        `(2) quant critique with halt trigger analysis, (3) COO recommendation. ` +
        `Clearly state if available data covers less than the requested period.`
      : "";
    const finalPrompt = userPrompt || defaultPrompt;
    const termTail   = getShellOutput();

    setTeamRunning(true);

    // Switch to the Team tab so the user sees the feed immediately
    activeAgent = "team";
    document.querySelectorAll(".tabBtn").forEach(b =>
      b.classList.toggle("active", b.getAttribute("data-agent") === "team"));
    showAgentTab("team");

    try {
      const res = await fetch(API_BASE + "/team/review", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          mode,
          prompt: finalPrompt,
          review_period: period,
          workspace: {
            terminal_tail: termTail,
            conversation_history: convHistory,
          },
        }),
      });
      const data = await res.json();
      activeTeamRunId = data.run_id;
      teamPollCursor  = 0;
      // Kick off polling immediately, then every 2 seconds
      await pollTeamReview();
      teamPollTimer = setInterval(pollTeamReview, 2000);
    } catch(err) {
      appendTeamEvent({t: _nowIso(), agent: "system", type: "error",
        content: "Failed to start team review: " + (err.message || String(err))});
      setTeamRunning(false);
    }
  }

  async function pollTeamReview() {
    if (!activeTeamRunId || teamRunCancelled) return;
    try {
      const res = await fetch(API_BASE + `/team/review/poll/${activeTeamRunId}?cursor=${teamPollCursor}`);
      const data = await res.json();
      if (data.events && data.events.length) {
        data.events.forEach(ev => appendTeamEvent(ev));
        teamPollCursor += data.events.length;
      }
      if (data.done || data.error) {
        clearInterval(teamPollTimer);
        teamPollTimer = null;
        activeTeamRunId = null;
        setTeamRunning(false);
      }
    } catch(_err) {
      // Network blip — keep polling unless cancelled
    }
  }

  function cancelTeamReview() {
    teamRunCancelled = true;
    if (teamPollTimer) { clearInterval(teamPollTimer); teamPollTimer = null; }
    appendTeamEvent({t: _nowIso(), agent: "system", type: "cancelled",
      content: "Run cancelled by user."});
    activeTeamRunId = null;
    setTeamRunning(false);
  }

  // ── Team review pre-assessment modal ──────────────────────────────────────
  const teamReviewModal   = document.getElementById("teamReviewModal");
  const trModalTitle      = document.getElementById("trModalTitle");
  const trModalContext    = document.getElementById("trModalContext");
  const trModalCancelBtn  = document.getElementById("trModalCancelBtn");
  const trModalStartBtn   = document.getElementById("trModalStartBtn");
  let _pendingTeamMode    = null;
  let _pendingTeamPeriod  = null;

  const _reviewTitles = {
    "quick":    "⚡ Quick Team Review",
    "detailed": "🔍 Detailed Team Review",
    "yearly":   "📅 2-Year Periodic Review",
  };

  function openTeamReviewModal(mode, period) {
    if (activeTeamRunId) return; // already running
    _pendingTeamMode   = mode;
    _pendingTeamPeriod = period;
    const key = (period === "2-year") ? "yearly" : mode;
    trModalTitle.textContent = _reviewTitles[key] || "Team Assessment";
    trModalContext.value = "";
    teamReviewModal.style.display = "flex";
    trModalContext.focus();
  }

  function closeTeamReviewModal() {
    teamReviewModal.style.display = "none";
    _pendingTeamMode  = null;
    _pendingTeamPeriod = null;
  }

  trModalCancelBtn.onclick = closeTeamReviewModal;
  teamReviewModal.addEventListener("click", e => { if (e.target === teamReviewModal) closeTeamReviewModal(); });

  trModalStartBtn.onclick = async () => {
    const extra  = trModalContext.value.trim();
    const mode   = _pendingTeamMode;
    const period = _pendingTeamPeriod;
    closeTeamReviewModal();
    await runTeamReview(mode, period, extra);
  };

  quickReviewBtn.onclick    = () => openTeamReviewModal("quick", "");
  detailedReviewBtn.onclick = () => openTeamReviewModal("detailed", "");
  yearlyReviewBtn.onclick   = () => openTeamReviewModal("detailed", "2-year");
  cancelReviewBtn.onclick   = cancelTeamReview;

  // ── Analytics tab ─────────────────────────────────────────────────────────

  const analyticsFetchBtn       = document.getElementById("analyticsFetchBtn");
  const analyticsLoadBtn        = document.getElementById("analyticsLoadBtn");
  const analyticsStatus         = document.getElementById("analyticsStatus");
  const analyticsBodyEl         = document.getElementById("analyticsBody");
  const analyticsEmptyEl        = document.getElementById("analyticsEmpty");
  const analyticsStatsEl        = document.getElementById("analyticsStats");
  const pnlChartWrapEl          = document.getElementById("pnlChartWrap");
  const equityChartWrapEl       = document.getElementById("equityChartWrap");
  const tradesTableSectionEl    = document.getElementById("tradesTableSection");

  function _renderAnalyticsSection(title, text) {
    const sec = document.createElement("div");
    sec.className = "analyticsSection";
    const h = document.createElement("div");
    h.className = "analyticsSectionTitle";
    h.textContent = title;
    sec.appendChild(h);
    const pre = document.createElement("div");
    pre.className = "analyticsRaw";
    pre.textContent = text || "(no data)";
    sec.appendChild(pre);
    return sec;
  }

  // ── Vanilla canvas line chart ──────────────────────────────────────────────
  function _drawLineChart(canvas, points, lineColor, fillColor) {
    const dpr  = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width  = rect.width  * dpr;
    canvas.height = rect.height * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    const W = rect.width, H = rect.height;
    const PL = 62, PR = 12, PT = 10, PB = 28;
    const plotW = W - PL - PR, plotH = H - PT - PB;
    if (!points.length) {
      ctx.fillStyle = "rgba(230,238,252,.35)";
      ctx.font = "12px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No data", W / 2, H / 2);
      return;
    }
    const vals = points.map(p => p.v);
    const minV = Math.min(...vals), maxV = Math.max(...vals);
    const rangeV = maxV === minV ? (Math.abs(maxV) || 1) : maxV - minV;
    const px = i  => PL + (i / Math.max(1, points.length - 1)) * plotW;
    const py = v  => PT + (1 - (v - minV) / rangeV) * plotH;
    // Grid + Y labels
    ctx.strokeStyle = "rgba(255,255,255,.06)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const gy = PT + (i / 4) * plotH;
      ctx.beginPath(); ctx.moveTo(PL, gy); ctx.lineTo(W - PR, gy); ctx.stroke();
      const gv = maxV - (i / 4) * rangeV;
      ctx.fillStyle = "rgba(230,238,252,.45)";
      ctx.font = "10px monospace";
      ctx.textAlign = "right";
      const label = Math.abs(gv) >= 1000 ? gv.toFixed(0) : gv.toFixed(2);
      ctx.fillText(label, PL - 4, gy + 4);
    }
    // Zero line
    if (minV < 0 && maxV > 0) {
      const zy = py(0);
      ctx.strokeStyle = "rgba(255,255,255,.20)";
      ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(PL, zy); ctx.lineTo(W - PR, zy); ctx.stroke();
      ctx.setLineDash([]);
    }
    // Fill area
    ctx.beginPath();
    ctx.moveTo(px(0), py(points[0].v));
    for (let i = 1; i < points.length; i++) ctx.lineTo(px(i), py(points[i].v));
    ctx.lineTo(px(points.length - 1), PT + plotH);
    ctx.lineTo(px(0), PT + plotH);
    ctx.closePath();
    ctx.fillStyle = fillColor;
    ctx.fill();
    // Line
    ctx.beginPath();
    ctx.moveTo(px(0), py(points[0].v));
    for (let i = 1; i < points.length; i++) ctx.lineTo(px(i), py(points[i].v));
    ctx.strokeStyle = lineColor;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();
    // X-axis labels
    if (points.length >= 2) {
      const idxs = [0, Math.floor((points.length - 1) / 2), points.length - 1];
      const aligns = ["left", "center", "right"];
      ctx.fillStyle = "rgba(230,238,252,.40)";
      ctx.font = "10px sans-serif";
      idxs.forEach((idx, n) => {
        ctx.textAlign = aligns[n];
        const label = points[idx].t.slice(5, 16).replace("T", " ");
        ctx.fillText(label, px(idx), H - 6);
      });
    }
  }

  // ── Stat card factory ──────────────────────────────────────────────────────
  function _statCard(label, value, cls) {
    const card = document.createElement("div");
    card.className = "statCard";
    const lbl = document.createElement("div");
    lbl.className = "statCardLabel";
    lbl.textContent = label;
    card.appendChild(lbl);
    const val = document.createElement("div");
    val.className = "statCardValue" + (cls ? " " + cls : "");
    val.textContent = value;
    card.appendChild(val);
    return card;
  }

  // ── Load Charts & Table from /trades and /pnl ─────────────────────────────
  analyticsLoadBtn.onclick = async () => {
    analyticsLoadBtn.disabled = true;
    analyticsStatus.textContent = "Loading…";
    analyticsBodyEl.querySelectorAll(".analyticsSection").forEach(el => el.remove());
    analyticsEmptyEl.style.display = "none";
    analyticsStatsEl.style.display = "none";
    analyticsStatsEl.innerHTML = "";
    pnlChartWrapEl.style.display = "none";
    equityChartWrapEl.style.display = "none";
    tradesTableSectionEl.style.display = "none";
    try {
      const [tradesRes, pnlRes] = await Promise.all([
        fetch(API_BASE + "/trades?limit=200"),
        fetch(API_BASE + "/pnl?limit=200"),
      ]);
      const tradesData = tradesRes.ok ? await tradesRes.json() : {trades: [], count: 0};
      const pnlData    = pnlRes.ok   ? await pnlRes.json()   : {snapshots: [], count: 0};
      // API returns newest-first; reverse to chronological order for charts
      const trades = (tradesData.trades    || []).slice().reverse();
      const snaps  = (pnlData.snapshots    || []).slice().reverse();

      // ── KPI stat cards ──────────────────────────────────────────────────
      const lastSnap  = snaps.length ? snaps[snaps.length - 1] : null;
      const latestPnl = lastSnap && lastSnap.total_pnl    != null ? lastSnap.total_pnl    : null;
      const latestEq  = lastSnap && lastSnap.equity       != null ? lastSnap.equity       : null;
      const latestDD  = lastSnap && lastSnap.drawdown     != null ? lastSnap.drawdown     : null;
      const latestSh  = lastSnap && lastSnap.sharpe_ratio != null ? lastSnap.sharpe_ratio : null;
      const buys  = trades.filter(t => t.side === "buy").length;
      const sells = trades.filter(t => t.side === "sell").length;
      [
        _statCard("Trades", trades.length, ""),
        _statCard("Buys / Sells", buys + " / " + sells, ""),
        _statCard("Total P&L",
          latestPnl != null ? (latestPnl >= 0 ? "+" : "") + latestPnl.toFixed(2) : "—",
          latestPnl != null ? (latestPnl >= 0 ? "pos" : "neg") : ""),
        _statCard("Equity", latestEq != null ? latestEq.toFixed(2) : "—", ""),
        _statCard("Drawdown",
          latestDD != null ? (latestDD * 100).toFixed(2) + "%" : "—",
          latestDD != null && latestDD > 0.05 ? "neg" : ""),
        _statCard("Sharpe",
          latestSh != null ? latestSh.toFixed(3) : "—",
          latestSh != null ? (latestSh >= 1 ? "pos" : latestSh < 0 ? "neg" : "") : ""),
      ].forEach(c => analyticsStatsEl.appendChild(c));
      analyticsStatsEl.style.display = "flex";

      // ── P&L line chart ──────────────────────────────────────────────────
      const pnlPts = snaps.filter(s => s.total_pnl != null).map(s => ({t: s.ts, v: s.total_pnl}));
      if (pnlPts.length) {
        pnlChartWrapEl.style.display = "";
        const cvs = document.getElementById("pnlChart");
        requestAnimationFrame(() => _drawLineChart(cvs, pnlPts, "#22c55e", "rgba(34,197,94,.12)"));
      }

      // ── Equity line chart ───────────────────────────────────────────────
      const eqPts = snaps.filter(s => s.equity != null).map(s => ({t: s.ts, v: s.equity}));
      if (eqPts.length) {
        equityChartWrapEl.style.display = "";
        const cvs = document.getElementById("equityChart");
        requestAnimationFrame(() => _drawLineChart(cvs, eqPts, "#60a5fa", "rgba(96,165,250,.10)"));
      }

      // ── Trade executions table ──────────────────────────────────────────
      if (trades.length) {
        tradesTableSectionEl.style.display = "";
        const wrap  = document.getElementById("tradesTableWrap");
        const table = document.createElement("table");
        table.className = "tradesTable";
        table.innerHTML =
          "<thead><tr>" +
          "<th>#</th><th>Timestamp</th><th>Symbol</th><th>Side</th>" +
          "<th>Size</th><th>Fill Price</th><th>Fee</th><th>Net P&L</th>" +
          "<th>Exchange</th><th>Tag</th><th>Strategy</th><th>Trade ID</th><th>Source</th>" +
          "</tr></thead>";
        const tbody = document.createElement("tbody");
        // Show newest first (up to 100 rows); trades is already chronological so slice the tail
        trades.slice(-100).reverse().forEach(t => {
          const tr = document.createElement("tr");
          const sideClass = (t.side || "").toLowerCase() === "buy" ? "buy" : "sell";
          const tagVal = (t.tag || "").toLowerCase();
          const tagClass = tagVal === "good" ? "good" : tagVal === "bad" ? "bad" : tagVal === "neutral" ? "neutral" : "";
          const tagLabel = tagClass ? tagVal : "—";
          const netPnlVal = t.net_pnl != null ? (t.net_pnl >= 0 ? "+" : "") + Number(t.net_pnl).toFixed(4) : "—";
          const netPnlCls = t.net_pnl != null ? (t.net_pnl >= 0 ? "pos" : "neg") : "";
          tr.innerHTML =
            "<td>" + escapeHtml(String(t.id != null ? t.id : "")) + "</td>" +
            "<td>" + escapeHtml((t.ts || "").replace("T", " ").slice(0, 19)) + "</td>" +
            "<td>" + escapeHtml(t.symbol || "") + "</td>" +
            "<td><span class='tradeSide " + sideClass + "'>" + escapeHtml(t.side || "") + "</span></td>" +
            "<td>" + escapeHtml(String(t.size != null ? t.size : "")) + "</td>" +
            "<td>" + escapeHtml(String(t.fill_price != null ? t.fill_price : "")) + "</td>" +
            "<td>" + escapeHtml(t.fee != null ? String(t.fee) : "—") + "</td>" +
            "<td class='" + netPnlCls + "'>" + escapeHtml(netPnlVal) + "</td>" +
            "<td>" + escapeHtml(t.exchange || "—") + "</td>" +
            "<td><span class='tradeTag " + tagClass + "'>" + escapeHtml(tagLabel) + "</span></td>" +
            "<td>" + escapeHtml(t.strategy || "—") + "</td>" +
            "<td style='max-width:120px;overflow:hidden;text-overflow:ellipsis'>" + escapeHtml(t.trade_id || "") + "</td>" +
            "<td>" + escapeHtml(t.source || "") + "</td>";
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        wrap.innerHTML = "";
        wrap.appendChild(table);
      }

      if (!pnlPts.length && !eqPts.length && !trades.length) {
        analyticsEmptyEl.textContent = "No trade or P&L data in the local log. Use SSH Probe for raw VPS data, or ensure the bot is posting to /trades/log and /pnl/log endpoints on this control server.";
        analyticsEmptyEl.style.display = "";
      }
      analyticsStatus.textContent = "Updated " + new Date().toLocaleTimeString();
    } catch(e) {
      analyticsBodyEl.appendChild(_renderAnalyticsSection("Error", e.message || String(e)));
      analyticsStatus.textContent = "Error";
    } finally {
      analyticsLoadBtn.disabled = false;
    }
  };

  // ── Kraken live pull ───────────────────────────────────────────────────────
  const krakenPullBtnEl       = document.getElementById("krakenPullBtn");
  const krakenLiveWrapEl      = document.getElementById("krakenLiveWrap");
  const krakenBalanceStatsEl  = document.getElementById("krakenBalanceStats");
  const krakenPositionsTableEl= document.getElementById("krakenPositionsTable");
  const krakenTradesTableEl   = document.getElementById("krakenTradesTable");

  krakenPullBtnEl.onclick = async () => {
    krakenPullBtnEl.disabled = true;
    analyticsStatus.textContent = "Fetching Kraken…";
    krakenLiveWrapEl.style.display = "none";
    try {
      const res = await fetch(API_BASE + "/exchange/kraken/live");
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        analyticsStatus.textContent = "Kraken error: HTTP " + res.status;
        krakenLiveWrapEl.style.display = "";
        krakenPositionsTableEl.innerHTML =
          "<div class='liveExchangeEmpty'>" + escapeHtml(err.detail || "Request failed") + "</div>";
        return;
      }
      const data = await res.json();
      krakenLiveWrapEl.style.display = "";
      krakenBalanceStatsEl.innerHTML = "";

      if (data.balance_error) {
        krakenBalanceStatsEl.innerHTML =
          "<div class='liveExchangeEmpty'>" + escapeHtml(data.balance_error) + "</div>";
      } else if (data.balance) {
        const b = data.balance;
        [
          _statCard("Equity", b.equity != null ? Number(b.equity).toFixed(2) : "—",
            b.equity != null && b.equity >= 0 ? "pos" : "neg"),
          _statCard("Unrealised P&L", b.unrealised_pnl != null ? (b.unrealised_pnl >= 0 ? "+" : "") + Number(b.unrealised_pnl).toFixed(4) : "—",
            b.unrealised_pnl != null ? (b.unrealised_pnl >= 0 ? "pos" : "neg") : ""),
          _statCard("Free Margin", b.free_margin != null ? Number(b.free_margin).toFixed(4) : "—", ""),
          _statCard("Margin Level", b.margin_level != null ? Number(b.margin_level).toFixed(2) + "%" : "—", ""),
        ].forEach(c => krakenBalanceStatsEl.appendChild(c));
      }

      if (data.positions_error) {
        krakenPositionsTableEl.innerHTML =
          "<div class='liveExchangeEmpty'>" + escapeHtml(data.positions_error) + "</div>";
      } else if (!data.positions || data.positions.length === 0) {
        krakenPositionsTableEl.innerHTML = "<div class='liveExchangeEmpty'>No open positions.</div>";
      } else {
        const table = document.createElement("table");
        table.className = "tradesTable";
        table.innerHTML =
          "<thead><tr><th>Position ID</th><th>Symbol</th><th>Side</th><th>Size</th>" +
          "<th>Entry Price</th><th>Fee</th><th>Net P&L</th><th>Unrealised P&L</th><th>Status</th></tr></thead>";
        const tbody = document.createElement("tbody");
        data.positions.forEach(p => {
          const sideClass = (p.side || "").toLowerCase() === "buy" ? "buy" : "sell";
          const unrPnl = p.unrealised_pnl != null ? (p.unrealised_pnl >= 0 ? "+" : "") + Number(p.unrealised_pnl).toFixed(4) : "—";
          const netPnl = p.net_pnl != null ? (p.net_pnl >= 0 ? "+" : "") + Number(p.net_pnl).toFixed(4) : "—";
          const tr = document.createElement("tr");
          tr.innerHTML =
            "<td style='max-width:100px;overflow:hidden;text-overflow:ellipsis'>" + escapeHtml(p.position_id || "") + "</td>" +
            "<td>" + escapeHtml(p.symbol || "") + "</td>" +
            "<td><span class='tradeSide " + sideClass + "'>" + escapeHtml(p.side || "") + "</span></td>" +
            "<td>" + escapeHtml(String(p.size != null ? p.size : "")) + "</td>" +
            "<td>" + escapeHtml(p.entry_price != null ? Number(p.entry_price).toFixed(4) : "—") + "</td>" +
            "<td>" + escapeHtml(p.fee != null ? String(p.fee) : "—") + "</td>" +
            "<td class='" + (p.net_pnl != null ? (p.net_pnl >= 0 ? "pos" : "neg") : "") + "'>" + escapeHtml(netPnl) + "</td>" +
            "<td class='" + (p.unrealised_pnl != null ? (p.unrealised_pnl >= 0 ? "pos" : "neg") : "") + "'>" + escapeHtml(unrPnl) + "</td>" +
            "<td>" + escapeHtml(p.status || "") + "</td>";
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        krakenPositionsTableEl.innerHTML = "";
        krakenPositionsTableEl.appendChild(table);
      }

      // ── Kraken recent trade history ────────────────────────────────────────
      if (data.trades_error) {
        krakenTradesTableEl.innerHTML =
          "<div class='liveExchangeEmpty'>" + escapeHtml(data.trades_error) + "</div>";
      } else if (!data.trades || data.trades.length === 0) {
        krakenTradesTableEl.innerHTML = "<div class='liveExchangeEmpty'>No recent trades found.</div>";
      } else {
        const tbl = document.createElement("table");
        tbl.className = "tradesTable";
        tbl.innerHTML =
          "<thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Size</th>" +
          "<th>Fill Price</th><th>Fee</th><th>Trade ID</th></tr></thead>";
        const tb = document.createElement("tbody");
        data.trades.forEach(t => {
          const sc = (t.side || "").toLowerCase() === "buy" ? "buy" : "sell";
          const tr = document.createElement("tr");
          tr.innerHTML =
            "<td>" + escapeHtml(t.ts || "") + "</td>" +
            "<td>" + escapeHtml(t.symbol || "") + "</td>" +
            "<td><span class='tradeSide " + sc + "'>" + escapeHtml(t.side || "") + "</span></td>" +
            "<td>" + escapeHtml(t.size != null ? String(t.size) : "—") + "</td>" +
            "<td>" + escapeHtml(t.fill_price != null ? Number(t.fill_price).toFixed(4) : "—") + "</td>" +
            "<td>" + escapeHtml(t.fee != null ? String(t.fee) : "—") + "</td>" +
            "<td style='max-width:120px;overflow:hidden;text-overflow:ellipsis'>" + escapeHtml(t.trade_id || "") + "</td>";
          tb.appendChild(tr);
        });
        tbl.appendChild(tb);
        krakenTradesTableEl.innerHTML = "";
        krakenTradesTableEl.appendChild(tbl);
      }

      analyticsStatus.textContent = "Kraken updated " + new Date().toLocaleTimeString();
    } catch(e) {
      krakenLiveWrapEl.style.display = "";
      krakenPositionsTableEl.innerHTML =
        "<div class='liveExchangeEmpty'>" + escapeHtml(e.message || String(e)) + "</div>";
      analyticsStatus.textContent = "Error";
    } finally {
      krakenPullBtnEl.disabled = false;
    }
  };

  // ── Alpaca live pull ───────────────────────────────────────────────────────
  const alpacaPullBtnEl        = document.getElementById("alpacaPullBtn");
  const alpacaLiveWrapEl       = document.getElementById("alpacaLiveWrap");
  const alpacaAccountStatsEl   = document.getElementById("alpacaAccountStats");
  const alpacaPositionsTableEl = document.getElementById("alpacaPositionsTable");
  const alpacaTradesTableEl    = document.getElementById("alpacaTradesTable");

  alpacaPullBtnEl.onclick = async () => {
    alpacaPullBtnEl.disabled = true;
    analyticsStatus.textContent = "Fetching Alpaca…";
    alpacaLiveWrapEl.style.display = "none";
    try {
      const res = await fetch(API_BASE + "/exchange/alpaca/live");
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        analyticsStatus.textContent = "Alpaca error: HTTP " + res.status;
        alpacaLiveWrapEl.style.display = "";
        alpacaPositionsTableEl.innerHTML =
          "<div class='liveExchangeEmpty'>" + escapeHtml(err.detail || "Request failed") + "</div>";
        return;
      }
      const data = await res.json();
      alpacaLiveWrapEl.style.display = "";
      alpacaAccountStatsEl.innerHTML = "";

      if (data.account_error) {
        alpacaAccountStatsEl.innerHTML =
          "<div class='liveExchangeEmpty'>" + escapeHtml(data.account_error) + "</div>";
      } else if (data.account) {
        const a = data.account;
        [
          _statCard("Equity", a.equity != null ? Number(a.equity).toFixed(2) : "—",
            a.equity != null && a.equity >= 0 ? "pos" : "neg"),
          _statCard("Portfolio Value", a.portfolio_value != null ? Number(a.portfolio_value).toFixed(2) : "—", ""),
          _statCard("Unrealised P&L", a.unrealised_pnl != null ? (a.unrealised_pnl >= 0 ? "+" : "") + Number(a.unrealised_pnl).toFixed(2) : "—",
            a.unrealised_pnl != null ? (a.unrealised_pnl >= 0 ? "pos" : "neg") : ""),
          _statCard("Realised P&L", a.realised_pnl != null ? (a.realised_pnl >= 0 ? "+" : "") + Number(a.realised_pnl).toFixed(2) : "—",
            a.realised_pnl != null ? (a.realised_pnl >= 0 ? "pos" : "neg") : ""),
          _statCard("Cash", a.cash != null ? Number(a.cash).toFixed(2) : "—", ""),
          _statCard("Buying Power", a.buying_power != null ? Number(a.buying_power).toFixed(2) : "—", ""),
        ].forEach(c => alpacaAccountStatsEl.appendChild(c));
      }

      if (data.positions_error) {
        alpacaPositionsTableEl.innerHTML =
          "<div class='liveExchangeEmpty'>" + escapeHtml(data.positions_error) + "</div>";
      } else if (!data.positions || data.positions.length === 0) {
        alpacaPositionsTableEl.innerHTML = "<div class='liveExchangeEmpty'>No open positions.</div>";
      } else {
        const table = document.createElement("table");
        table.className = "tradesTable";
        table.innerHTML =
          "<thead><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry Price</th>" +
          "<th>Current Price</th><th>Cost Basis</th><th>Market Value</th><th>Unrealised P&L</th><th>Unrealised %</th><th>Realised P&L</th><th>Today Change</th></tr></thead>";
        const tbody = document.createElement("tbody");
        data.positions.forEach(p => {
          const sideClass = (p.side || "").toLowerCase() === "long" ? "buy" : "sell";
          const unrPnl = p.unrealised_pnl != null ? (p.unrealised_pnl >= 0 ? "+" : "") + Number(p.unrealised_pnl).toFixed(2) : "—";
          const unrPct = p.unrealised_pnl_pct != null ? (p.unrealised_pnl_pct * 100).toFixed(2) + "%" : "—";
          const realPnl = p.realised_pnl != null ? (p.realised_pnl >= 0 ? "+" : "") + Number(p.realised_pnl).toFixed(2) : "—";
          const chg = p.change_today != null ? (p.change_today >= 0 ? "+" : "") + (p.change_today * 100).toFixed(2) + "%" : "—";
          const tr = document.createElement("tr");
          tr.innerHTML =
            "<td>" + escapeHtml(p.symbol || "") + "</td>" +
            "<td><span class='tradeSide " + sideClass + "'>" + escapeHtml(p.side || "") + "</span></td>" +
            "<td>" + escapeHtml(String(p.size != null ? p.size : "")) + "</td>" +
            "<td>" + escapeHtml(p.entry_price != null ? Number(p.entry_price).toFixed(4) : "—") + "</td>" +
            "<td>" + escapeHtml(p.current_price != null ? Number(p.current_price).toFixed(4) : "—") + "</td>" +
            "<td>" + escapeHtml(p.cost_basis != null ? Number(p.cost_basis).toFixed(2) : "—") + "</td>" +
            "<td>" + escapeHtml(p.market_value != null ? Number(p.market_value).toFixed(2) : "—") + "</td>" +
            "<td class='" + (p.unrealised_pnl != null ? (p.unrealised_pnl >= 0 ? "pos" : "neg") : "") + "'>" + escapeHtml(unrPnl) + "</td>" +
            "<td class='" + (p.unrealised_pnl_pct != null ? (p.unrealised_pnl_pct >= 0 ? "pos" : "neg") : "") + "'>" + escapeHtml(unrPct) + "</td>" +
            "<td class='" + (p.realised_pnl != null ? (p.realised_pnl >= 0 ? "pos" : "neg") : "") + "'>" + escapeHtml(realPnl) + "</td>" +
            "<td class='" + (p.change_today != null ? (p.change_today >= 0 ? "pos" : "neg") : "") + "'>" + escapeHtml(chg) + "</td>";
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        alpacaPositionsTableEl.innerHTML = "";
        alpacaPositionsTableEl.appendChild(table);
      }

      // ── Alpaca recent trade history ────────────────────────────────────────
      if (data.trades_error) {
        alpacaTradesTableEl.innerHTML =
          "<div class='liveExchangeEmpty'>" + escapeHtml(data.trades_error) + "</div>";
      } else if (!data.trades || data.trades.length === 0) {
        alpacaTradesTableEl.innerHTML = "<div class='liveExchangeEmpty'>No recent trades found.</div>";
      } else {
        const tbl = document.createElement("table");
        tbl.className = "tradesTable";
        tbl.innerHTML =
          "<thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th>" +
          "<th>Fill Price</th><th>Trade ID</th></tr></thead>";
        const tb = document.createElement("tbody");
        data.trades.forEach(t => {
          const sc = (t.side || "").toLowerCase() === "buy" ? "buy" : "sell";
          const tr = document.createElement("tr");
          tr.innerHTML =
            "<td>" + escapeHtml(t.ts || "") + "</td>" +
            "<td>" + escapeHtml(t.symbol || "") + "</td>" +
            "<td><span class='tradeSide " + sc + "'>" + escapeHtml(t.side || "") + "</span></td>" +
            "<td>" + escapeHtml(t.size != null ? String(t.size) : "—") + "</td>" +
            "<td>" + escapeHtml(t.fill_price != null ? Number(t.fill_price).toFixed(4) : "—") + "</td>" +
            "<td style='max-width:140px;overflow:hidden;text-overflow:ellipsis'>" + escapeHtml(t.trade_id || "") + "</td>";
          tb.appendChild(tr);
        });
        tbl.appendChild(tb);
        alpacaTradesTableEl.innerHTML = "";
        alpacaTradesTableEl.appendChild(tbl);
      }

      analyticsStatus.textContent = "Alpaca updated " + new Date().toLocaleTimeString();
    } catch(e) {
      alpacaLiveWrapEl.style.display = "";
      alpacaPositionsTableEl.innerHTML =
        "<div class='liveExchangeEmpty'>" + escapeHtml(e.message || String(e)) + "</div>";
      analyticsStatus.textContent = "Error";
    } finally {
      alpacaPullBtnEl.disabled = false;
    }
  };
  analyticsFetchBtn.onclick = async () => {
    analyticsFetchBtn.disabled = true;
    analyticsStatus.textContent = "Fetching…";
    analyticsBodyEl.querySelectorAll(".analyticsSection").forEach(el => el.remove());
    analyticsEmptyEl.style.display = "none";
    try {
      const res = await fetch(API_BASE + "/ops/report?report_id=per_trade_analytics", {method: "POST"});
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        analyticsBodyEl.appendChild(_renderAnalyticsSection(
          "Error",
          err.detail || `HTTP ${res.status}`
        ));
        analyticsStatus.textContent = "Error";
        return;
      }
      const data = await res.json();
      const raw = (data.output || "").trim();

      if (!raw || raw.startsWith("[no ") || raw.startsWith("[SSH not") || raw.startsWith("[Unknown")) {
        analyticsEmptyEl.textContent = raw || "No analytics data available. Ensure the bot is running and trade logs exist on the VPS.";
        analyticsEmptyEl.style.display = "";
        analyticsStatus.textContent = "No data";
        return;
      }

      // Split output by section headers (--- cmd... ---) and render each
      const sections = raw.split(/\\n(?=---)/);
      sections.forEach(sec => {
        const lines = sec.split("\\n");
        const header = lines[0].startsWith("---") ? lines[0].replace(/^---\\s*/, "").replace(/\\s*---$/, "").trim() : "Results";
        const body = lines.slice(1).join("\\n").trim();
        if (body && body !== "(empty)") {
          analyticsBodyEl.appendChild(_renderAnalyticsSection(header, body));
        }
      });

      analyticsStatus.textContent = "Updated " + new Date().toLocaleTimeString();
    } catch(e) {
      analyticsBodyEl.appendChild(_renderAnalyticsSection("Error", e.message || String(e)));
      analyticsStatus.textContent = "Error";
    } finally {
      analyticsFetchBtn.disabled = false;
    }
  };

  // ── Memory cockpit ─────────────────────────────────────────────────────────

  const memRefreshBtnEl    = document.getElementById("memRefreshBtn");
  const memInvalidateBtnEl = document.getElementById("memInvalidateBtn");
  const memSaveBtnEl       = document.getElementById("memSaveBtn");
  const memoryEditorEl     = document.getElementById("memoryEditor");
  const memoryMetaEl       = document.getElementById("memoryMeta");
  const memoryEventsListEl = document.getElementById("memoryEventsList");

  let activeMemoryAgent = "pnl";

  document.querySelectorAll(".memoryAgentBtn").forEach(btn => {
    btn.addEventListener("click", () => {
      activeMemoryAgent = btn.getAttribute("data-memory-agent");
      document.querySelectorAll(".memoryAgentBtn").forEach(b =>
        b.classList.toggle("active", b === btn));
      loadMemoryCockpit();
    });
  });

  async function loadMemoryCockpit() {
    memRefreshBtnEl.disabled = true;
    memoryMetaEl.textContent = "Loading…";
    memoryEditorEl.value = "";
    memoryEventsListEl.innerHTML = "";
    try {
      const res = await fetch(API_BASE + "/memory/" + activeMemoryAgent);
      if (!res.ok) {
        memoryMetaEl.textContent = "Error loading memory: HTTP " + res.status;
        return;
      }
      const data = await res.json();
      const snap = data.snapshot || {};
      memoryEditorEl.value = JSON.stringify(snap, null, 2);
      const fp = data.fingerprint || "(none)";
      const evCount = (data.recent_events || []).length;
      memoryMetaEl.textContent = "fingerprint: " + fp + "  ·  " + evCount + " recent event(s)";
      (data.recent_events || []).forEach(ev => {
        const row = document.createElement("div");
        row.className = "memEventRow " + (ev.kind || "");
        const ts = (ev.ts || "").replace("T", " ").replace("+00:00", "").replace("Z", "");
        const payload = JSON.stringify(ev.payload || {});
        row.textContent = "[" + ts + "]  " + (ev.kind || "") + "  " + payload;
        memoryEventsListEl.appendChild(row);
      });
    } catch(err) {
      memoryMetaEl.textContent = "Error: " + (err.message || String(err));
    } finally {
      memRefreshBtnEl.disabled = false;
    }
  }

  memRefreshBtnEl.onclick = loadMemoryCockpit;

  memSaveBtnEl.onclick = async () => {
    let parsed;
    try {
      parsed = JSON.parse(memoryEditorEl.value || "{}");
    } catch(e) {
      alert("Invalid JSON — please fix before saving:\\n" + e.message);
      return;
    }
    memSaveBtnEl.disabled = true;
    memSaveBtnEl.textContent = "⏳ Saving…";
    try {
      const res = await fetch(API_BASE + "/memory/" + activeMemoryAgent, {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({snapshot: parsed}),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert("Save failed: " + (err.detail || "HTTP " + res.status));
        return;
      }
      memSaveBtnEl.textContent = "✅ Saved";
      setTimeout(() => { memSaveBtnEl.textContent = "💾 Save"; }, 2000);
      await loadMemoryCockpit();
    } catch(err) {
      alert("Error: " + (err.message || String(err)));
      memSaveBtnEl.textContent = "💾 Save";
    } finally {
      memSaveBtnEl.disabled = false;
    }
  };

  memInvalidateBtnEl.onclick = async () => {
    if (!confirm("Invalidate memory for " + activeMemoryAgent + "? This clears the snapshot and forces a fresh probe on the next agent interaction.")) return;
    memInvalidateBtnEl.disabled = true;
    try {
      const res = await fetch(API_BASE + "/memory/" + activeMemoryAgent + "/invalidate", {
        method: "POST",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        alert("Invalidate failed: " + (err.detail || "HTTP " + res.status));
        return;
      }
      await loadMemoryCockpit();
    } catch(err) {
      alert("Error: " + (err.message || String(err)));
    } finally {
      memInvalidateBtnEl.disabled = false;
    }
  };
</script>
</body>
</html>
"""
    if gateway_script:
        page = page.replace("</head>", gateway_script + "</head>", 1)
    return page


@app.post("/copilot")
def copilot_issue(req: CopilotRequest):
    token = settings.github_token
    if not token:
        return {"error": "GITHUB_TOKEN is not configured. Set the GITHUB_TOKEN environment variable."}

    # Resolve target repo: client-supplied value wins if it is in the allowed list.
    repo_full = (
        req.target_repo
        if (req.target_repo and req.target_repo in ALLOWED_REPOS)
        else settings.github_repo
    )

    if not repo_full or len(repo_full.split("/", 1)) != 2:
        return {"error": "Target repo must be in 'owner/repo' format"}

    title_text = req.goal.strip()[:80] if req.goal.strip() else "Task from OpenClaw UI"
    title = f"[Copilot] {title_text}"
    body = _build_issue_body(req)

    result = create_github_issue(
        title=title,
        body=body,
        repo_full=repo_full,
        labels=["copilot"],
        token=token,
        assign_copilot=True,
    )
    if result is None:
        return {"error": "GitHub API request failed. Check GITHUB_TOKEN and repo permissions."}

    return {
        "issue_url": result["issue_url"],
        "issue_number": result["issue_number"],
        "assignment": result["assignment"],
        "used_repo": result["used_repo"],
    }


@app.get("/copilot/poll/{issue_number}")
def copilot_poll(issue_number: int, repo: str = ""):
    token = settings.github_token
    if not token:
        return {"pr_url": None}

    # Use client-supplied repo if it is in the allowed list, else fall back to server default.
    repo_full = repo if (repo and repo in ALLOWED_REPOS) else settings.github_repo
    parts = repo_full.split("/", 1)
    if len(parts) != 2:
        return {"pr_url": None}

    owner, repo_name = parts
    url = f"https://api.github.com/repos/{owner}/{repo_name}/issues/{issue_number}/timeline"

    try:
        r = _requests.get(url, headers=_gh_headers(token), timeout=10)
        if not r.ok:
            return {"pr_url": None}
        for event in r.json():
            if event.get("event") == "cross-referenced":
                source = event.get("source", {})
                if source.get("type") == "pull_request":
                    pr = source.get("issue", {})
                    if pr.get("html_url"):
                        return {"pr_url": pr["html_url"]}
        return {"pr_url": None}
    except Exception:
        return {"pr_url": None}


@app.post("/message")
def message(msg: Message):
    return handle_message(msg.text)


@app.post("/agent/message")
def agent_message(msg: AgentMessage):
    return handle_agent_message(msg.agent, msg.text, msg.workspace)


@app.post("/team/review")
def team_review_start(req: TeamReviewRequest):
    mode = req.mode if req.mode in ("quick", "detailed") else "quick"
    workspace = dict(req.workspace)
    if req.review_period:
        workspace["review_period"] = req.review_period
    run_id = start_team_review(mode, req.prompt, workspace)
    return {"run_id": run_id}


@app.get("/team/review/poll/{run_id}")
def team_review_poll(run_id: str, cursor: int = 0):
    return get_team_review_events(run_id, cursor)


@app.post("/team/proposal/confirm")
def proposal_confirm(req: ProposalConfirmRequest):
    """Create a GitHub issue from an operator-confirmed team review proposal.

    The operator must supply a repo that is in the allowed list.  Labels default
    to ``["copilot", "team-review"]`` when not specified by the caller.
    """
    repo = (req.repo or "").strip()
    if repo not in ALLOWED_REPOS:
        raise HTTPException(
            status_code=422,
            detail=f"Repo '{repo}' is not in the allowed list.",
        )
    title = (req.title or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title is required")

    result = create_github_issue(
        title=title,
        body=req.body or "",
        repo_full=repo,
        labels=req.labels or ["copilot", "team-review"],
        assign_copilot=req.assign_copilot,
    )
    if result is None:
        return {"error": "GitHub API request failed. Check GITHUB_TOKEN and repo permissions."}
    return result


# ── VPS wrapper endpoints ─────────────────────────────────────────────────────

_VPS_SERVICES: dict = {}
try:
    import json as _json_mod
    import pathlib as _pathlib
    _services_path = _pathlib.Path(__file__).parent / "control_contract" / "services.json"
    _VPS_SERVICES = _json_mod.loads(_services_path.read_text())
except FileNotFoundError:
    _logger.warning("control_contract/services.json not found — VPS service registry is empty.")
except Exception as _exc:
    _logger.warning("Failed to load control_contract/services.json: %s", _exc)


@app.get("/vps/services")
def vps_services():
    """Return the canonical service registry from control_contract/services.json."""
    return _VPS_SERVICES


@app.get("/vps/status/{service_id}")
def vps_status(service_id: str):
    """Return the systemd status of a registered service via the VPS wrapper."""
    if service_id not in _VPS_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service '{service_id}'.")
    try:
        return _vps.status(service_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/vps/start/{service_id}")
def vps_start(service_id: str):
    """Start a registered service via the VPS wrapper (services only, not timers)."""
    svc = _VPS_SERVICES.get(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Unknown service '{service_id}'.")
    if svc.get("type") == "timer":
        raise HTTPException(
            status_code=422,
            detail=f"'{service_id}' is a timer-backed job — use /vps/run/{service_id} instead.",
        )
    try:
        return _vps.start(service_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/vps/stop/{service_id}")
def vps_stop(service_id: str):
    """Stop a registered service via the VPS wrapper."""
    if service_id not in _VPS_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service '{service_id}'.")
    try:
        return _vps.stop(service_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/vps/restart/{service_id}")
def vps_restart(service_id: str):
    """Restart a registered service via the VPS wrapper (services only, not timers)."""
    svc = _VPS_SERVICES.get(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Unknown service '{service_id}'.")
    if svc.get("type") == "timer":
        raise HTTPException(
            status_code=422,
            detail=f"'{service_id}' is a timer-backed job — use /vps/run/{service_id} instead.",
        )
    try:
        return _vps.restart(service_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/vps/run/{service_id}")
def vps_run(service_id: str):
    """Trigger a oneshot run of a timer-backed job via the VPS wrapper."""
    svc = _VPS_SERVICES.get(service_id)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Unknown service '{service_id}'.")
    if svc.get("type") != "timer":
        raise HTTPException(
            status_code=422,
            detail=f"'{service_id}' is a long-running service — use /vps/start/{service_id} instead.",
        )
    try:
        return _vps.run_once(service_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/vps/logs/{service_id}")
def vps_logs(service_id: str, lines: int = 200):
    """Return the last N log lines for a registered service via the VPS wrapper."""
    if service_id not in _VPS_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service '{service_id}'.")
    lines = max(1, min(lines, 1000))
    try:
        return {"service_id": service_id, "logs": _vps.logs(service_id, lines=lines)}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ── Trade log endpoints ───────────────────────────────────────────────────────

@app.post("/trades/log")
def trades_log(req: TradeLogRequest):
    """Persist a trade execution to the on-disk trade log.

    ``ts`` defaults to the current UTC time when omitted.
    """
    ts = (req.ts or "").strip() or _trade_log_now_iso()
    row_id = _trade_log.log_trade(
        ts=ts,
        symbol=req.symbol,
        side=req.side,
        size=req.size,
        fill_price=req.fill_price,
        trade_id=req.trade_id,
        source=req.source,
        exchange=req.exchange,
        open_ts=req.open_ts,
        close_ts=req.close_ts,
        entry_price=req.entry_price,
        exit_price=req.exit_price,
        gross_pnl=req.gross_pnl,
        net_pnl=req.net_pnl,
        fee=req.fee,
        tag=req.tag,
        signal=req.signal,
        strategy=req.strategy,
        config_version=req.config_version,
        annotation=req.annotation,
    )
    return {"id": row_id, "ts": ts, "status": "logged"}


@app.get("/trades")
def trades_get(limit: int = 50):
    """Return the most recent trade executions from the persistent log.

    ``limit`` is capped at 500 to prevent accidental large responses.
    """
    limit = max(1, min(limit, 500))
    rows = _trade_log.get_recent_trades(limit=limit)
    return {"trades": rows, "count": len(rows)}


@app.post("/pnl/log")
def pnl_log(req: PnlLogRequest):
    """Persist a P&L snapshot to the on-disk log.

    ``ts`` defaults to the current UTC time when omitted.
    """
    ts = (req.ts or "").strip() or _trade_log_now_iso()
    row_id = _trade_log.log_pnl_snapshot(
        ts=ts,
        total_pnl=req.total_pnl,
        equity=req.equity,
        drawdown=req.drawdown,
        realised_pnl=req.realised_pnl,
        unrealised_pnl=req.unrealised_pnl,
        sharpe_ratio=req.sharpe_ratio,
        source=req.source,
    )
    return {"id": row_id, "ts": ts, "status": "logged"}


@app.get("/pnl")
def pnl_get(limit: int = 50):
    """Return the most recent P&L snapshots from the persistent log.

    ``limit`` is capped at 500 to prevent accidental large responses.
    """
    limit = max(1, min(limit, 500))
    rows = _trade_log.get_recent_pnl(limit=limit)
    return {"snapshots": rows, "count": len(rows)}


@app.get("/trades/health")
def trades_health():
    """Return the current trade inactivity health status.

    ``is_inactive`` is True when no trade has been recorded within the
    configured window (default 12 h, override with
    OPENCLAW_TRADE_INACTIVITY_HOURS env var).
    """
    return _trade_log.get_inactivity_status()


# ── Trade tagging endpoint ─────────────────────────────────────────────────────

_VALID_TAGS = frozenset({"good", "bad", "neutral", ""})


@app.patch("/trades/{row_id}/tag")
def trades_tag(row_id: int, req: TradeTagRequest):
    """Update the tag and optional annotation for a trade execution row.

    ``tag`` must be one of: 'good', 'bad', 'neutral', or '' (clears tag).
    """
    tag = (req.tag or "").strip().lower()
    if tag not in _VALID_TAGS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid tag '{tag}'. Valid values: good, bad, neutral, or empty string.",
        )
    updated = _trade_log.update_trade_tag(row_id=row_id, tag=tag, annotation=req.annotation)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Trade row id={row_id} not found.")
    return {"id": row_id, "tag": tag, "annotation": req.annotation, "status": "updated"}


# ── Live exchange data endpoints ───────────────────────────────────────────────


@app.get("/exchange/kraken/live")
def exchange_kraken_live():
    """Fetch live Kraken open positions, trade balance, and recent trade history via the Kraken REST API.

    Requires KRAKEN_API_KEY and KRAKEN_SECRET_KEY environment variables.
    """
    positions_raw = _fetch_kraken_positions()
    balance_raw = _fetch_kraken_balance()
    trades_raw = _fetch_kraken_trades(limit=20)

    # Decouple from any exception-tainted strings before building the response.
    if isinstance(positions_raw, list):
        positions_out: list = positions_raw
        positions_err: str | None = None
    else:
        _logger.warning("Kraken positions fetch error: %s", positions_raw)
        positions_out = []
        positions_err = _safe_error_msg(positions_raw)

    if isinstance(balance_raw, dict):
        balance_out: dict | None = balance_raw
        balance_err: str | None = None
    else:
        _logger.warning("Kraken balance fetch error: %s", balance_raw)
        balance_out = None
        balance_err = _safe_error_msg(balance_raw)

    if isinstance(trades_raw, list):
        trades_out: list = trades_raw
        trades_err: str | None = None
    else:
        _logger.warning("Kraken trades fetch error: %s", trades_raw)
        trades_out = []
        trades_err = _safe_error_msg(trades_raw)

    return {
        "positions": positions_out,
        "positions_error": positions_err,
        "balance": balance_out,
        "balance_error": balance_err,
        "trades": trades_out,
        "trades_error": trades_err,
        "source": "kraken",
    }


@app.get("/exchange/alpaca/live")
def exchange_alpaca_live():
    """Fetch live Alpaca open positions, account summary, and recent trade history via the Alpaca REST API.

    Requires ALPACA_API_KEY and ALPACA_SECRET_KEY environment variables.
    """
    positions_raw = _fetch_alpaca_positions()
    account_raw = _fetch_alpaca_account()
    trades_raw_a = _fetch_alpaca_trades(limit=20)

    # Decouple from any exception-tainted strings before building the response.
    if isinstance(positions_raw, list):
        positions_out_a: list = positions_raw
        positions_err_a: str | None = None
    else:
        _logger.warning("Alpaca positions fetch error: %s", positions_raw)
        positions_out_a = []
        positions_err_a = _safe_error_msg(positions_raw)

    if isinstance(account_raw, dict):
        account_out: dict | None = account_raw
        account_err: str | None = None
    else:
        _logger.warning("Alpaca account fetch error: %s", account_raw)
        account_out = None
        account_err = _safe_error_msg(account_raw)

    if isinstance(trades_raw_a, list):
        trades_out_a: list = trades_raw_a
        trades_err_a: str | None = None
    else:
        _logger.warning("Alpaca trades fetch error: %s", trades_raw_a)
        trades_out_a = []
        trades_err_a = _safe_error_msg(trades_raw_a)

    return {
        "positions": positions_out_a,
        "positions_error": positions_err_a,
        "account": account_out,
        "account_error": account_err,
        "trades": trades_out_a,
        "trades_error": trades_err_a,
        "source": "alpaca",
    }


# ── Cheap Chat endpoint ───────────────────────────────────────────────────────

@app.post("/cheap-chat")
def cheap_chat(req: CheapChatRequest):
    """Send a message to a cheap inference provider and return the reply."""
    reply = _cheap_chat(
        message=req.message,
        provider=req.provider,
        history=req.history,
    )
    if reply.startswith("❌"):
        return {"error": reply}
    return {"reply": reply}


# ── Auth & OpenAI Chat endpoints ──────────────────────────────────────────────

_AUTH_COOKIE = "openclaw_session"
# Set SECURE_COOKIES=true in production (behind HTTPS/Caddy).
# Leave unset or set to false for local HTTP development.
_SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "false").lower() in ("1", "true", "yes")


def _current_user(token: str | None) -> str | None:
    """Return the email from the session cookie, or None if unauthenticated."""
    if not token:
        return None
    return _auth.decode_token(token)


class LoginRequest(BaseModel):
    email: str
    password: str


class ChangePasswordRequest(BaseModel):
    email: str
    old_password: str
    new_password: str


class ChatRequest(BaseModel):
    user_id: str = ""        # optional; defaults to the authenticated email
    message: str
    session_id: str = ""     # multi-session support; empty = legacy mode
    provider: str = "openai" # which LLM provider to use
    model: str = ""          # override provider's default model
    web_search: bool = False  # prepend Brave Search results to message


class ClearHistoryRequest(BaseModel):
    user_id: str = ""


class CreateSessionRequest(BaseModel):
    provider: str = "openai"
    model: str = ""


@app.get("/login", response_class=HTMLResponse)
def login_page():
    """Serve the login page."""
    return HTMLResponse(_LOGIN_HTML)


@app.post("/auth/login")
def auth_login(req: LoginRequest, response: Response):
    if not _auth.authenticate(req.email, req.password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = _auth.create_token(req.email)
    response.set_cookie(
        _AUTH_COOKIE,
        token,
        httponly=True,
        secure=_SECURE_COOKIES,
        samesite="lax",
        max_age=7 * 24 * 3600,
    )
    return {"ok": True, "email": req.email}


@app.post("/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(
        _AUTH_COOKIE,
        httponly=True,
        secure=_SECURE_COOKIES,
        samesite="lax",
    )
    return {"ok": True}


@app.post("/auth/change-password")
def auth_change_password(
    req: ChangePasswordRequest,
    openclaw_session: str | None = Cookie(default=None),
):
    caller = _current_user(openclaw_session)
    if caller != req.email:
        raise HTTPException(status_code=403, detail="Forbidden")
    if len(req.new_password) < 8:
        raise HTTPException(status_code=422, detail="New password must be at least 8 characters")
    ok = _auth.change_password(req.email, req.old_password, req.new_password)
    if not ok:
        raise HTTPException(status_code=401, detail="Old password is incorrect")
    return {"ok": True}


@app.get("/admin/users")
def admin_users(openclaw_session: str | None = Cookie(default=None)):
    """Return all registered users (email + created_at). Requires an active session.

    Password hashes are never returned.  Only authenticated operators can call
    this endpoint — unauthenticated requests receive 401.
    """
    caller = _current_user(openclaw_session)
    if not caller:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"users": _auth.list_users()}


@app.get("/chat/providers")
def chat_providers(openclaw_session: str | None = Cookie(default=None)):
    """Return available LLM providers and their models."""
    if not _current_user(openclaw_session):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"providers": _multi_chat.list_providers()}


@app.get("/chat/sessions")
def chat_sessions(openclaw_session: str | None = Cookie(default=None)):
    """Return all chat sessions for the current user."""
    caller = _current_user(openclaw_session)
    if not caller:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"sessions": _multi_chat.get_sessions(caller)}


@app.post("/chat/sessions")
def create_chat_session(
    req: CreateSessionRequest,
    openclaw_session: str | None = Cookie(default=None),
):
    """Create a new chat session."""
    caller = _current_user(openclaw_session)
    if not caller:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = _multi_chat.create_session(caller, provider=req.provider, model=req.model)
    return session


@app.delete("/chat/sessions/{session_id}")
def delete_chat_session(
    session_id: str,
    openclaw_session: str | None = Cookie(default=None),
):
    """Delete a chat session and all its messages."""
    caller = _current_user(openclaw_session)
    if not caller:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ok = _multi_chat.delete_session(session_id, caller)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


@app.post("/chat/sessions/{session_id}/clear")
def clear_chat_session(
    session_id: str,
    openclaw_session: str | None = Cookie(default=None),
):
    """Clear all messages in a session without deleting the session."""
    caller = _current_user(openclaw_session)
    if not caller:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _multi_chat.clear_session(session_id, caller)
    return {"ok": True}


@app.post("/chat")
def openai_chat(
    req: ChatRequest,
    openclaw_session: str | None = Cookie(default=None),
):
    """Send a message to the selected LLM and return the reply with memory."""
    caller = _current_user(openclaw_session)
    # Allow bot calls with an explicit user_id even without a session cookie.
    user_id = req.user_id.strip() if req.user_id.strip() else caller
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not req.message.strip():
        raise HTTPException(status_code=422, detail="message must not be empty")
    try:
        if req.session_id.strip():
            # Multi-session path — use multi_chat_feature
            reply = _multi_chat.chat(
                user_id=user_id,
                message=req.message,
                session_id=req.session_id.strip(),
                provider=req.provider or "openai",
                model=req.model or "",
                web_search=req.web_search,
            )
        else:
            # Legacy path (Telegram bot, old clients) — use chat_feature
            reply = _chat.chat(user_id, req.message)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM error: {exc}") from exc
    return {"reply": reply, "user_id": user_id}


@app.post("/chat/clear")
def openai_chat_clear(
    req: ClearHistoryRequest,
    openclaw_session: str | None = Cookie(default=None),
):
    """Clear conversation history for a user."""
    caller = _current_user(openclaw_session)
    user_id = req.user_id.strip() if req.user_id.strip() else caller
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    _chat.clear_history(user_id)
    return {"ok": True, "user_id": user_id}


@app.get("/chat-web", response_class=HTMLResponse)
def chat_web(openclaw_session: str | None = Cookie(default=None)):
    """Serve the authenticated web chat interface."""
    email = _current_user(openclaw_session)
    if not email:
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_CHAT_WEB_HTML.replace("__EMAIL__", json.dumps(email)))


# ── Login page HTML ───────────────────────────────────────────────────────────

_LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>OpenClaw — Sign in</title>
  <style>
    :root{
      --bg:#0b0f14;--panel:#0f1621;--border:rgba(255,255,255,.08);
      --text:#e6eefc;--muted:rgba(230,238,252,.55);
      --accent:#22c55e;--accent2:#16a34a;
      --sans:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
    }
    *{box-sizing:border-box;margin:0;padding:0;}
    html,body{height:100%;font-family:var(--sans);background:radial-gradient(1200px 600px at 30% 0%,rgba(34,197,94,.10),transparent 55%),var(--bg);color:var(--text);}
    .wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px;}
    .card{background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.015));border:1px solid var(--border);border-radius:24px;padding:40px 36px;width:100%;max-width:380px;box-shadow:0 20px 60px rgba(0,0,0,.5);}
    .logo{display:flex;align-items:center;gap:10px;margin-bottom:28px;}
    .logo svg{flex-shrink:0;}
    .logo span{font-size:1.25rem;font-weight:700;letter-spacing:.3px;}
    h1{font-size:1.1rem;font-weight:600;margin-bottom:6px;}
    p.sub{color:var(--muted);font-size:.85rem;margin-bottom:28px;}
    label{display:block;font-size:.82rem;color:var(--muted);margin-bottom:6px;margin-top:16px;}
    input{width:100%;background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:10px;padding:11px 14px;color:var(--text);font-size:.95rem;outline:none;transition:border .15s;}
    input:focus{border-color:var(--accent);}
    button{margin-top:24px;width:100%;background:var(--accent);color:#0b0f14;font-weight:700;font-size:.95rem;border:none;border-radius:12px;padding:13px;cursor:pointer;transition:background .15s;}
    button:hover{background:var(--accent2);}
    #err{color:#f87171;font-size:.85rem;margin-top:14px;display:none;}
  </style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="logo">
      <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
        <rect width="32" height="32" rx="8" fill="#22c55e" fill-opacity=".15"/>
        <path d="M8 22 L16 10 L24 22" stroke="#22c55e" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="16" cy="10" r="2.5" fill="#22c55e"/>
      </svg>
      <span>OpenClaw</span>
    </div>
    <h1>Sign in</h1>
    <p class="sub">Access your AI chat assistant</p>
    <form id="loginForm">
      <label for="email">Email</label>
      <input id="email" type="email" autocomplete="email" required placeholder="you@example.com"/>
      <label for="pass">Password</label>
      <input id="pass" type="password" autocomplete="current-password" required placeholder="••••••••"/>
      <button type="submit">Sign in</button>
    </form>
    <div id="err"></div>
  </div>
</div>
<script>
  document.getElementById("loginForm").addEventListener("submit", async e => {
    e.preventDefault();
    const err = document.getElementById("err");
    err.style.display = "none";
    const email = document.getElementById("email").value.trim();
    const password = document.getElementById("pass").value;
    try {
      const res = await fetch("/auth/login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({email, password}),
      });
      if (res.ok) {
        window.location.href = "/chat-web";
      } else {
        const data = await res.json().catch(() => ({}));
        err.textContent = data.detail || "Login failed";
        err.style.display = "block";
      }
    } catch {
      err.textContent = "Network error — please try again";
      err.style.display = "block";
    }
  });
</script>
</body>
</html>"""

# ── Web chat HTML ─────────────────────────────────────────────────────────────

_CHAT_WEB_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>OpenClaw — Chat</title>
  <style>
    :root{
      --bg:#0b0f14;--panel:#0f1621;--panel2:#0c121b;--sidebar:#0d1420;
      --border:rgba(255,255,255,.08);--text:#e6eefc;
      --muted:rgba(230,238,252,.50);--accent:#22c55e;--accent2:#16a34a;
      --bubbleUser:#1d2a3a;--bubbleAgent:#101b27;--danger:#f87171;
      --sans:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
      --mono:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Courier New",monospace;
      --sidebar-w:260px;
    }
    *{box-sizing:border-box;margin:0;padding:0;}
    html,body{height:100%;font-family:var(--sans);
      background:radial-gradient(1200px 600px at 30% 0%,rgba(34,197,94,.08),transparent 55%),
                 radial-gradient(900px 600px at 85% 20%,rgba(59,130,246,.06),transparent 55%),
                 var(--bg);
      color:var(--text);overflow:hidden;}

    /* ── Shell layout ── */
    .shell{height:100vh;display:flex;}

    /* ── Sidebar ── */
    .sidebar{width:var(--sidebar-w);background:var(--sidebar);border-right:1px solid var(--border);
      display:flex;flex-direction:column;flex-shrink:0;transition:transform .2s;}
    .sidebar-header{padding:14px 12px 10px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;}
    .logo-mark{width:26px;height:26px;background:rgba(34,197,94,.15);border-radius:6px;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
    .app-name{font-weight:700;font-size:.95rem;flex:1;}
    .btn-new{background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.3);border-radius:8px;
      color:var(--accent);font-size:.8rem;font-weight:600;padding:5px 10px;cursor:pointer;
      white-space:nowrap;transition:all .15s;}
    .btn-new:hover{background:rgba(34,197,94,.25);}
    .sessions-list{flex:1;overflow-y:auto;padding:6px;}
    .sessions-list::-webkit-scrollbar{width:3px;}
    .sessions-list::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);}
    .session-item{display:flex;align-items:center;gap:6px;padding:8px 10px;border-radius:10px;
      cursor:pointer;transition:background .15s;group-hover:block;}
    .session-item:hover{background:rgba(255,255,255,.06);}
    .session-item.active{background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.2);}
    .session-title{flex:1;font-size:.84rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
    .session-del{opacity:0;flex-shrink:0;background:none;border:none;color:var(--danger);
      font-size:.9rem;cursor:pointer;padding:2px 4px;border-radius:4px;transition:opacity .15s;}
    .session-item:hover .session-del{opacity:1;}
    .sidebar-footer{padding:10px 12px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:6px;}
    .sidebar-footer .user-email{font-size:.78rem;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}

    /* ── Main area ── */
    .main{flex:1;display:flex;flex-direction:column;min-width:0;}

    /* ── Top bar ── */
    .topbar{padding:10px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;
      gap:8px;background:rgba(255,255,255,.02);flex-shrink:0;flex-wrap:wrap;}
    .topbar-left{display:flex;align-items:center;gap:8px;flex:1;min-width:0;}
    .hamburger{display:none;background:none;border:none;color:var(--muted);cursor:pointer;padding:4px;}
    .session-label{font-size:.88rem;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:200px;}
    .topbar-right{display:flex;align-items:center;gap:6px;flex-wrap:wrap;}
    select.provider-sel,select.model-sel{background:rgba(255,255,255,.06);border:1px solid var(--border);
      border-radius:8px;color:var(--text);font-size:.8rem;padding:5px 8px;cursor:pointer;outline:none;
      transition:border .15s;}
    select.provider-sel:focus,select.model-sel:focus{border-color:var(--accent);}
    select.provider-sel option,select.model-sel option{background:#1a2130;color:var(--text);}
    .btn-sm{background:rgba(255,255,255,.06);border:1px solid var(--border);border-radius:8px;
      color:var(--muted);font-size:.8rem;padding:5px 10px;cursor:pointer;transition:all .15s;white-space:nowrap;}
    .btn-sm:hover{background:rgba(255,255,255,.10);color:var(--text);}
    .btn-sm.danger:hover{background:rgba(248,113,113,.15);border-color:rgba(248,113,113,.4);color:#fca5a5;}
    .btn-search{background:rgba(255,255,255,.06);border:1px solid var(--border);border-radius:8px;
      color:var(--muted);font-size:.8rem;padding:5px 10px;cursor:pointer;transition:all .15s;
      display:flex;align-items:center;gap:4px;white-space:nowrap;}
    .btn-search.active{background:rgba(34,197,94,.15);border-color:rgba(34,197,94,.4);color:var(--accent);}
    .btn-search:hover{background:rgba(255,255,255,.10);color:var(--text);}

    /* ── Messages ── */
    .messages{flex:1;overflow-y:auto;padding:20px 16px;display:flex;flex-direction:column;gap:12px;}
    .messages::-webkit-scrollbar{width:4px;}
    .messages::-webkit-scrollbar-track{background:transparent;}
    .messages::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:4px;}
    .bubble{max-width:78%;padding:12px 16px;border-radius:16px;font-size:.93rem;line-height:1.6;word-break:break-word;}
    .bubble.user{background:var(--bubbleUser);border:1px solid rgba(34,197,94,.15);align-self:flex-end;border-bottom-right-radius:4px;}
    .bubble.assistant{background:var(--bubbleAgent);border:1px solid var(--border);align-self:flex-start;border-bottom-left-radius:4px;}
    .bubble .sender{font-size:.72rem;font-weight:600;margin-bottom:6px;text-transform:uppercase;letter-spacing:.6px;}
    .bubble.user .sender{color:var(--accent);}
    .bubble.assistant .sender{color:var(--muted);}
    /* Markdown styles inside bubbles */
    .bubble h1,.bubble h2,.bubble h3{margin:.5em 0 .3em;font-size:1em;font-weight:700;}
    .bubble p{margin:.3em 0;}
    .bubble ul,.bubble ol{padding-left:1.4em;margin:.3em 0;}
    .bubble li{margin:.15em 0;}
    .bubble pre{background:rgba(0,0,0,.35);border-radius:8px;padding:10px 12px;margin:.5em 0;
      overflow-x:auto;font-family:var(--mono);font-size:.82rem;}
    .bubble code{background:rgba(0,0,0,.25);border-radius:4px;padding:1px 5px;font-family:var(--mono);font-size:.85em;}
    .bubble pre code{background:none;padding:0;font-size:inherit;}
    .bubble a{color:var(--accent);text-decoration:underline;}
    .bubble strong{font-weight:700;}
    .bubble em{font-style:italic;}
    .bubble hr{border:none;border-top:1px solid var(--border);margin:.5em 0;}
    .typing{display:none;align-self:flex-start;padding:10px 14px;background:var(--bubbleAgent);
      border:1px solid var(--border);border-radius:12px;border-bottom-left-radius:4px;}
    .typing span{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--muted);
      margin:0 2px;animation:blink 1.2s infinite;}
    .typing span:nth-child(2){animation-delay:.2s;}
    .typing span:nth-child(3){animation-delay:.4s;}
    @keyframes blink{0%,80%,100%{opacity:.2;}40%{opacity:1;}}

    /* ── Composer ── */
    .composer{padding:10px 14px 14px;border-top:1px solid var(--border);background:rgba(255,255,255,.01);flex-shrink:0;}
    .composer-row{display:flex;gap:8px;align-items:flex-end;}
    textarea{flex:1;background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:14px;
      padding:11px 14px;color:var(--text);font-size:.93rem;font-family:var(--sans);resize:none;outline:none;
      transition:border .15s;line-height:1.5;max-height:160px;}
    textarea:focus{border-color:var(--accent);}
    .send-btn{background:var(--accent);border:none;border-radius:12px;width:42px;height:42px;cursor:pointer;
      display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:background .15s;}
    .send-btn:hover{background:var(--accent2);}
    .send-btn:disabled{background:rgba(34,197,94,.25);cursor:not-allowed;}
    .send-btn svg{flex-shrink:0;}

    /* ── Modal ── */
    .modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:100;align-items:center;justify-content:center;}
    .modal-bg.open{display:flex;}
    .modal{background:var(--panel);border:1px solid var(--border);border-radius:20px;padding:28px;width:100%;max-width:360px;}
    .modal h2{font-size:1rem;font-weight:600;margin-bottom:16px;}
    .modal label{display:block;font-size:.8rem;color:var(--muted);margin-bottom:5px;margin-top:12px;}
    .modal input{width:100%;background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:8px;padding:9px 12px;color:var(--text);font-size:.9rem;outline:none;}
    .modal input:focus{border-color:var(--accent);}
    .modal-btns{display:flex;gap:8px;margin-top:20px;}
    .modal-btns button{flex:1;padding:10px;border-radius:10px;border:none;font-weight:600;cursor:pointer;font-size:.88rem;}
    .modal-btns .save{background:var(--accent);color:#0b0f14;}
    .modal-btns .cancel{background:rgba(255,255,255,.07);color:var(--text);}
    #pwErr{color:#f87171;font-size:.82rem;margin-top:10px;display:none;}
    #pwOk{color:var(--accent);font-size:.82rem;margin-top:10px;display:none;}

    /* ── Responsive ── */
    @media(max-width:700px){
      .sidebar{position:fixed;inset:0 auto 0 0;z-index:50;transform:translateX(-100%);}
      .sidebar.open{transform:translateX(0);}
      .hamburger{display:block;}
      .session-label{max-width:120px;}
    }
  </style>
</head>
<body>
<div class="shell">

  <!-- Sidebar -->
  <nav class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <div class="logo-mark">
        <svg width="14" height="14" viewBox="0 0 32 32" fill="none">
          <path d="M8 22 L16 10 L24 22" stroke="#22c55e" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
          <circle cx="16" cy="10" r="3" fill="#22c55e"/>
        </svg>
      </div>
      <span class="app-name">OpenClaw AI</span>
      <button class="btn-new" onclick="newSession()">+ New</button>
    </div>
    <div class="sessions-list" id="sessionsList"></div>
    <div class="sidebar-footer">
      <div class="user-email" id="userEmailSidebar"></div>
      <button class="btn-sm" onclick="openPwModal()">Change password</button>
      <button class="btn-sm danger" onclick="logout()">Sign out</button>
    </div>
  </nav>

  <!-- Main -->
  <div class="main">
    <!-- Top bar -->
    <div class="topbar">
      <div class="topbar-left">
        <button class="hamburger" id="hamburger" onclick="toggleSidebar()" title="Toggle sidebar">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>
          </svg>
        </button>
        <span class="session-label" id="sessionLabel">New chat</span>
      </div>
      <div class="topbar-right">
        <select class="provider-sel" id="providerSel" onchange="onProviderChange()" title="LLM provider"></select>
        <select class="model-sel" id="modelSel" title="Model"></select>
        <button class="btn-sm danger" onclick="clearSession()">Clear</button>
      </div>
    </div>

    <!-- Messages -->
    <div class="messages" id="messages">
      <div class="bubble assistant">
        <div class="sender">Assistant</div>
        Hello! I'm your OpenClaw AI assistant. Select a provider above and start chatting.
      </div>
      <div class="typing" id="typing"><span></span><span></span><span></span></div>
    </div>

    <!-- Composer -->
    <div class="composer">
      <div class="composer-row">
        <textarea id="input" rows="1" placeholder="Message OpenClaw AI…" autofocus></textarea>
        <button class="send-btn" id="sendBtn" title="Send">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
            <path d="M22 2L11 13" stroke="#0b0f14" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M22 2L15 22L11 13L2 9L22 2Z" stroke="#0b0f14" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </button>
      </div>
    </div>
  </div>
</div>

<!-- Change password modal -->
<div class="modal-bg" id="pwModal">
  <div class="modal">
    <h2>Change password</h2>
    <label>Current password</label>
    <input type="password" id="oldPw" placeholder="••••••••"/>
    <label>New password (min 8 chars)</label>
    <input type="password" id="newPw" placeholder="••••••••"/>
    <label>Confirm new password</label>
    <input type="password" id="confirmPw" placeholder="••••••••"/>
    <div id="pwErr"></div>
    <div id="pwOk"></div>
    <div class="modal-btns">
      <button class="cancel" onclick="closePwModal()">Cancel</button>
      <button class="save" onclick="submitPwChange()">Save</button>
    </div>
  </div>
</div>

<script>
const EMAIL = __EMAIL__;
document.getElementById("userEmailSidebar").textContent = EMAIL;

// ── State ──────────────────────────────────────────────────────────────────
let currentSessionId = "";
let providers = {};   // { openai: { name, models, default_model }, ... }
let sessions = [];    // [{ id, title, provider, model, updated_at }, ...]

const messagesEl  = document.getElementById("messages");
const inputEl     = document.getElementById("input");
const sendBtn     = document.getElementById("sendBtn");
const typingEl    = document.getElementById("typing");
const providerSel = document.getElementById("providerSel");
const modelSel    = document.getElementById("modelSel");
const sessionLabel = document.getElementById("sessionLabel");
const sessionsList = document.getElementById("sessionsList");

// ── Markdown renderer ─────────────────────────────────────────────────────
function renderMarkdown(text) {
  // Escape HTML in the raw text first so user content can never inject markup
  let s = text
    .replace(/&/g,"&amp;")
    .replace(/</g,"&lt;")
    .replace(/>/g,"&gt;");

  // Fenced code blocks (``` ... ```) — content already HTML-escaped, safe to wrap
  s = s.replace(/```([\\w]*?)\\n([\\s\\S]*?)```/g, (_,lang,code) =>
    `<pre><code>${code}</code></pre>`);
  s = s.replace(/```([\\s\\S]*?)```/g, (_,code) => `<pre><code>${code}</code></pre>`);

  // Inline code
  s = s.replace(/`([^`\\n]+)`/g, "<code>$1</code>");

  // Headers
  s = s.replace(/^### (.+)$/gm, "<h3>$1</h3>");
  s = s.replace(/^## (.+)$/gm, "<h2>$1</h2>");
  s = s.replace(/^# (.+)$/gm, "<h1>$1</h1>");

  // Bold / italic
  s = s.replace(/\\*\\*\\*(.+?)\\*\\*\\*/g, "<strong><em>$1</em></strong>");
  s = s.replace(/\\*\\*(.+?)\\*\\*/g, "<strong>$1</strong>");
  s = s.replace(/\\*(.+?)\\*/g, "<em>$1</em>");
  s = s.replace(/_([^_\\n]+)_/g, "<em>$1</em>");

  // Horizontal rule
  s = s.replace(/^---+$/gm, "<hr/>");

  // Unordered lists — group consecutive <li> runs into <ul> blocks
  s = s.replace(/^[ \\t]*[-*+] (.+)$/gm, "<li>$1</li>");
  s = s.replace(/(<li>[\\s\\S]*?<\\/li>)(\\n<li>[\\s\\S]*?<\\/li>)*/g,
    m => "<ul>" + m + "</ul>");

  // Ordered lists — convert then wrap consecutive runs in <ol>
  s = s.replace(/^[ \\t]*\\d+\\. (.+)$/gm, "<lio>$1</lio>");
  s = s.replace(/(<lio>[\\s\\S]*?<\\/lio>)(\\n<lio>[\\s\\S]*?<\\/lio>)*/g,
    m => "<ol>" + m.replace(/<\\/?lio>/g, m2 => m2.replace("lio","li")) + "</ol>");

  // Links — only allow http/https URLs to prevent javascript: XSS.
  // Escape `"` in the URL to prevent attribute-boundary injection.
  s = s.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, (_, linkText, url) => {
    const safeUrl = (/^https?:\\/\\//i.test(url) ? url : "#").replace(/"/g, "&quot;");
    return `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer">${linkText}</a>`;
  });

  // Line breaks (not inside pre blocks)
  s = s.replace(/\\n/g, "<br/>");

  // Clean up extra br after block elements
  s = s.replace(/(<\\/(?:h[123]|hr|pre|ul|ol)>)<br\\/>/g, "$1");
  s = s.replace(/<br\\/>(\\s*)(<(?:h[123]|pre|ul|ol)>)/g, "$2");

  return s;
}

// ── Provider / model selectors ─────────────────────────────────────────────
async function loadProviders() {
  try {
    const res = await fetch("/chat/providers");
    if (!res.ok) return;
    const data = await res.json();
    providers = data.providers || {};
    providerSel.innerHTML = "";
    for (const [id, info] of Object.entries(providers)) {
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = info.name;
      providerSel.appendChild(opt);
    }
    populateModels(providerSel.value);
  } catch(e) {}
}

function populateModels(providerId) {
  const info = providers[providerId];
  if (!info) return;
  modelSel.innerHTML = "";
  for (const m of info.models) {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    if (m === info.default_model) opt.selected = true;
    modelSel.appendChild(opt);
  }
}

function onProviderChange() {
  populateModels(providerSel.value);
}

// ── Sessions ───────────────────────────────────────────────────────────────
async function loadSessions() {
  try {
    const res = await fetch("/chat/sessions");
    if (!res.ok) return;
    const data = await res.json();
    sessions = data.sessions || [];
    renderSessionsList();
    if (sessions.length === 0) {
      await newSession();
    } else if (!currentSessionId) {
      await switchSession(sessions[0].id);
    }
  } catch(e) {}
}

function renderSessionsList() {
  sessionsList.innerHTML = "";
  for (const s of sessions) {
    const item = document.createElement("div");
    item.className = "session-item" + (s.id === currentSessionId ? " active" : "");
    item.dataset.id = s.id;

    const title = document.createElement("span");
    title.className = "session-title";
    title.textContent = s.title || "New chat";
    title.title = s.title || "New chat";

    const del = document.createElement("button");
    del.className = "session-del";
    del.textContent = "✕";
    del.title = "Delete session";
    del.addEventListener("click", e => { e.stopPropagation(); deleteSession(s.id); });

    item.appendChild(title);
    item.appendChild(del);
    item.addEventListener("click", () => switchSession(s.id));
    sessionsList.appendChild(item);
  }
}

async function newSession() {
  try {
    const res = await fetch("/chat/sessions", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ provider: providerSel.value || "openai", model: modelSel.value || "" }),
    });
    if (!res.ok) return;
    const session = await res.json();
    sessions.unshift(session);
    renderSessionsList();
    await switchSession(session.id);
  } catch(e) {}
}

async function switchSession(sessionId) {
  currentSessionId = sessionId;
  const s = sessions.find(x => x.id === sessionId);
  sessionLabel.textContent = (s && s.title && s.title !== "New chat") ? s.title : "New chat";
  // Update provider/model dropdowns to match session settings
  if (s) {
    if (s.provider && providerSel.querySelector(`option[value="${s.provider}"]`)) {
      providerSel.value = s.provider;
      populateModels(s.provider);
    }
    if (s.model && modelSel.querySelector(`option[value="${s.model}"]`)) {
      modelSel.value = s.model;
    }
  }
  renderSessionsList();
  await loadSessionMessages(sessionId);
}

async function loadSessionMessages(sessionId) {
  // Clear current messages (except typing indicator)
  Array.from(messagesEl.querySelectorAll(".bubble")).forEach(b => b.remove());
  messagesEl.appendChild(typingEl);
  // Load history from server via a GET call to a new endpoint
  // For now we rely on in-memory display — messages are shown as they arrive.
  // A full history-load endpoint can be added later.
  // Show welcome bubble
  const welcome = makeBubble("assistant", "Hello! I'm your OpenClaw AI assistant. How can I help you?");
  messagesEl.insertBefore(welcome, typingEl);
  scrollBottom();
}

async function deleteSession(sessionId) {
  if (!confirm("Delete this conversation?")) return;
  try {
    const res = await fetch(`/chat/sessions/${sessionId}`, {method:"DELETE"});
    if (!res.ok) return;
    sessions = sessions.filter(s => s.id !== sessionId);
    renderSessionsList();
    if (currentSessionId === sessionId) {
      currentSessionId = "";
      if (sessions.length > 0) {
        await switchSession(sessions[0].id);
      } else {
        await newSession();
      }
    }
  } catch(e) {}
}

// ── Messaging ──────────────────────────────────────────────────────────────
function scrollBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function makeBubble(role, text) {
  const div = document.createElement("div");
  div.className = "bubble " + role;
  const sender = document.createElement("div");
  sender.className = "sender";
  sender.textContent = role === "user" ? "You" : "Assistant";
  div.appendChild(sender);
  const content = document.createElement("div");
  content.innerHTML = role === "assistant" ? renderMarkdown(text) : escapeHtml(text).replace(/\\n/g,"<br/>");
  div.appendChild(content);
  return div;
}

function escapeHtml(t) {
  return t.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function addBubble(role, text) {
  messagesEl.insertBefore(makeBubble(role, text), typingEl);
  scrollBottom();
}

async function send() {
  const text = inputEl.value.trim();
  if (!text) return;
  if (!currentSessionId) {
    addBubble("assistant", "⚠️ No active session — click **+ New** to start a conversation.");
    return;
  }
  inputEl.value = "";
  inputEl.style.height = "auto";
  sendBtn.disabled = true;
  addBubble("user", text);
  typingEl.style.display = "flex";
  scrollBottom();
  try {
    const res = await fetch("/chat", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        message: text,
        session_id: currentSessionId,
        provider: providerSel.value || "openai",
        model: modelSel.value || "",
        web_search: true,
      }),
    });
    if (res.status === 401) { window.location.href = "/login"; return; }
    const data = await res.json();
    const reply = data.reply || data.detail || data.error || "(no response)";
    typingEl.style.display = "none";
    addBubble("assistant", reply);
    // Update session title in sidebar after first message
    const s = sessions.find(x => x.id === currentSessionId);
    if (s && (s.title === "New chat" || !s.title)) {
      s.title = text.slice(0, 50);
      sessionLabel.textContent = s.title;
      renderSessionsList();
    }
  } catch(e) {
    typingEl.style.display = "none";
    addBubble("assistant", "⚠️ Network error — please try again.");
  } finally {
    sendBtn.disabled = false;
    inputEl.focus();
  }
}

// ── Controls ─────────────────────────────────────────────────────────────────

async function clearSession() {
  if (!currentSessionId) return;
  if (!confirm("Clear all messages in this conversation? The session will remain.")) return;
  try {
    await fetch(`/chat/sessions/${currentSessionId}/clear`, {method:"POST"});
  } catch(e) {}
  // Remove message bubbles from view
  Array.from(messagesEl.querySelectorAll(".bubble")).forEach(b => b.remove());
  messagesEl.appendChild(typingEl);
  addBubble("assistant", "Conversation cleared. How can I help you?");
}

function toggleSidebar() {
  document.getElementById("sidebar").classList.toggle("open");
}

// ── Input ──────────────────────────────────────────────────────────────────
inputEl.addEventListener("input", () => {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
});
inputEl.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
sendBtn.addEventListener("click", send);

// ── Auth ───────────────────────────────────────────────────────────────────
async function logout() {
  await fetch("/auth/logout", {method:"POST"});
  window.location.href = "/login";
}

function openPwModal() {
  document.getElementById("pwModal").classList.add("open");
  document.getElementById("oldPw").focus();
}
function closePwModal() {
  document.getElementById("pwModal").classList.remove("open");
  ["oldPw","newPw","confirmPw"].forEach(id => document.getElementById(id).value = "");
  document.getElementById("pwErr").style.display = "none";
  document.getElementById("pwOk").style.display = "none";
}
async function submitPwChange() {
  const oldPw = document.getElementById("oldPw").value;
  const newPw = document.getElementById("newPw").value;
  const confirmPw = document.getElementById("confirmPw").value;
  const errEl = document.getElementById("pwErr");
  const okEl = document.getElementById("pwOk");
  errEl.style.display = "none"; okEl.style.display = "none";
  if (newPw !== confirmPw) { errEl.textContent = "Passwords do not match"; errEl.style.display = "block"; return; }
  if (newPw.length < 8) { errEl.textContent = "Password must be at least 8 characters"; errEl.style.display = "block"; return; }
  const res = await fetch("/auth/change-password", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({email: EMAIL, old_password: oldPw, new_password: newPw}),
  });
  if (res.ok) {
    okEl.textContent = "Password changed successfully!";
    okEl.style.display = "block";
    setTimeout(closePwModal, 1500);
  } else {
    const data = await res.json().catch(()=>({}));
    errEl.textContent = data.detail || "Failed to change password";
    errEl.style.display = "block";
  }
}

// ── Init ───────────────────────────────────────────────────────────────────
(async () => {
  await loadProviders();
  await loadSessions();
})();
</script>
</body>
</html>"""
