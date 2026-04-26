import requests as _requests

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from openclaw_control.config import settings
from openclaw_control.service import handle_message, handle_agent_message, start_team_review, get_team_review_events, start_vibe_run, get_vibe_run

app = FastAPI()


class Message(BaseModel):
    text: str


_ALLOWED_REPOS = {
    "leeheggan-droid/openclaw-crypto",
    "leeheggan-droid/alpaca_orb_bite_bot",
    "leeheggan-droid/LinkedIn_Data_Centre_News",
    "leeheggan-droid/openclaw-control",
}


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


class VibePlanRequest(BaseModel):
    goal: str
    workspace: dict = {}


class VibeExecuteRequest(BaseModel):
    command: str


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


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
        f"- **Host:** {settings.ssh_host or '(not configured)'}",
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
def config():
    return {
        "ssh_host": settings.ssh_host,
        "repo_dir": settings.repo_dir,
        "vibe_workdir": settings.vibe_workdir or settings.repo_dir,
        "allowed_repos": sorted(_ALLOWED_REPOS),
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return """
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

    /* Vibe execution gateway */
    .vibePad{
      flex:1;
      display:none;
      flex-direction:column;
      overflow:hidden;
    }
    .vibeInputSection{
      padding:12px;
      border-bottom:1px solid var(--border);
      background:rgba(255,255,255,.01);
      display:flex;
      flex-direction:column;
      gap:8px;
    }
    .vibeLabel{
      font-size:11px;
      color:var(--muted);
      font-weight:600;
      letter-spacing:.3px;
      text-transform:uppercase;
      margin-bottom:2px;
      display:block;
    }
    .vibeTextInput{
      width:100%;
      box-sizing:border-box;
      background:rgba(0,0,0,.22);
      border:1px solid var(--border);
      border-radius:10px;
      padding:8px 10px;
      color:var(--text);
      font-family:var(--mono);
      font-size:13px;
      outline:none;
    }
    .vibeTextInput:focus{border-color:rgba(34,197,94,.4);}
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
    .vibeActions{
      display:flex;
      gap:8px;
      flex-wrap:wrap;
    }
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
    .vibeFeed{
      flex:1;
      overflow:auto;
      padding:10px 12px;
      display:flex;
      flex-direction:column;
      gap:6px;
    }
    .vibeFeed::-webkit-scrollbar{width:10px;}
    .vibeFeed::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);border-radius:999px;}
    .vibeFeedRow{
      font-family:var(--mono);
      font-size:12px;
      white-space:pre-wrap;
      padding:8px 10px;
      background:rgba(0,0,0,.18);
      border:1px solid var(--border);
      border-radius:10px;
      color:var(--text);
      line-height:1.45;
    }
    .vibeFeedRow.info{color:rgba(147,197,253,.85);border-color:rgba(59,130,246,.2);}
    .vibeFeedRow.done{color:rgba(134,239,172,.85);border-color:rgba(34,197,94,.2);}
    .vibeFeedRow.err {color:rgba(252,165,165,.85);border-color:rgba(251,113,133,.2);}
  </style>
</head>

<body>
  <div class="app">
    <!-- LEFT -->
    <section class="card" id="leftCard">
      <div class="cardHeader">
        <div class="title">
          <span style="width:10px;height:10px;border-radius:999px;background:var(--accent);display:inline-block"></span>
          Agents
        </div>
        <div class="agentTabs">
          <button class="tabBtn active" data-agent="main">Main</button>
          <button class="tabBtn" data-agent="pnl">P&amp;L</button>
          <button class="tabBtn" data-agent="quant">Quant</button>
          <button class="tabBtn" data-agent="coo">COO</button>
          <button class="tabBtn" data-agent="vibe">Vibe</button>
          <button class="tabBtn" data-agent="team">Team</button>
        </div>
        <div class="badge" id="statusBadge">ready</div>
      </div>

      <div class="teamBtnsBar">
        <button class="teamBtn" id="quickReviewBtn">⚡ Quick team review</button>
        <button class="teamBtn" id="detailedReviewBtn">🔍 Detailed team review</button>
        <button class="teamBtn" id="yearlyReviewBtn">📅 Yearly review</button>
        <button class="teamBtn cancelBtn" id="cancelReviewBtn" style="display:none">✕ Cancel run</button>
      </div>

      <div id="chat-main" class="chatBody"></div>
      <div id="chat-pnl"  class="chatBody" style="display:none"></div>
      <div id="chat-quant" class="chatBody" style="display:none"></div>
      <div id="chat-coo"  class="chatBody" style="display:none"></div>

      <div class="teamFeed" id="teamFeed"></div>

      <!-- Vibe execution gateway panel -->
      <div class="vibePad" id="vibePad">
        <div class="vibeInputSection">
          <div>
            <label class="vibeLabel" for="vibeGoalInput">Goal</label>
            <textarea class="vibeTextarea" id="vibeGoalInput" rows="2" placeholder="Describe what you want to achieve…"></textarea>
          </div>
          <div>
            <label class="vibeLabel" for="vibeCommandInput">Shell Command</label>
            <textarea class="vibeTextarea" id="vibeCommandInput" rows="3" placeholder="AI will plan a shell command — or type one directly"></textarea>
          </div>
          <div class="vibeActions">
            <button class="vibeBtn vibeSecondaryBtn" id="vibePlanBtn">✨ Plan with AI</button>
            <button class="vibeBtn vibePrimaryBtn" id="vibeExecuteBtn">▶ Approve &amp; Execute</button>
          </div>
        </div>
        <div class="vibeApprovalBanner" id="vibeApprovalBanner">
          <div class="vibeApprovalTitle">⚠️ Review &amp; Confirm Vibe Execution</div>
          <div class="vibeApprovalCmd" id="vibeApprovalCmd"></div>
          <div class="vibeApprovalBtns">
            <button class="vibeBtn vibePrimaryBtn" id="vibeConfirmBtn">✅ Confirm &amp; Execute</button>
            <button class="vibeBtn vibeDangerBtn" id="vibeCancelApprovalBtn">✗ Cancel</button>
          </div>
        </div>
        <div class="vibeFeed" id="vibeFeed"></div>
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
          <div>Enter = send • Shift+Enter = newline • <code>/copilot &lt;goal&gt;</code></div>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
            <span>Shell: <code>!uptime</code>, <code>!docker ps</code></span>
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
        </div>
      </div>

      <div class="termBody" id="terminal"></div>

      <div class="termControls">
        <button class="pill" onclick="runQuick('uptime')">uptime</button>
        <button class="pill" onclick="runQuick('whoami')">whoami</button>
        <button class="pill" onclick="runQuick('docker ps')">docker ps</button>
        <button class="pill" onclick="runQuick('docker compose ps')">docker compose ps</button>
        <button class="pill" onclick="runQuick('ls -la')">ls -la</button>
        <button class="pill" onclick="runQuick('docker logs --tail=200 openclaw-orchestrator')">logs last 200</button>
        <button class="pill" onclick="runQuick('timeout 30 docker logs -f openclaw-orchestrator 2>/dev/null || docker logs --tail=200 openclaw-orchestrator')">logs follow 30s</button>
        <button class="pill" onclick="confirmDockerRefresh()">docker refresh</button>
        <button class="pill" onclick="clearTerminal()">clear</button>
      </div>
    </section>
  </div>

<script>
  // --- DOM references ---
  const CHAT_PANES = {
    main:  document.getElementById("chat-main"),
    pnl:   document.getElementById("chat-pnl"),
    quant: document.getElementById("chat-quant"),
    coo:   document.getElementById("chat-coo"),
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
  const AGENT_LABELS = {main: "Main AI", pnl: "P&L", quant: "Quant", coo: "COO"};
  const AGENT_STORE_KEYS = {
    main:  "openclaw_chat_main_v1",
    pnl:   "openclaw_chat_pnl_v1",
    quant: "openclaw_chat_quant_v1",
    coo:   "openclaw_chat_coo_v1",
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
  const vibePadEl  = document.getElementById("vibePad");
  const composerEl = document.querySelector(".chatComposer");

  function showAgentTab(ag) {
    const isTeam = ag === "team";
    const isVibe = ag === "vibe";
    // Show the right chat pane (or none for team/vibe) — no re-render
    for (const [key, pane] of Object.entries(CHAT_PANES)) {
      pane.style.display = (!isTeam && !isVibe && key === ag) ? "" : "none";
    }
    teamFeedEl.style.display     = isTeam ? "flex" : "none";
    vibePadEl.style.display      = isVibe ? "flex" : "none";
    composerEl.style.display     = (isTeam || isVibe) ? "none" : "";
    if (isTeam) {
      renderTeamFeed();
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
    histories[activeAgent].push({role:"user", text:`Attached: ${f.name}`, extraHTML: extra});
    saveHistory(activeAgent);
    fileInput.value = "";
  };

  // Attach image (preview)
  imgInput.onchange = async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
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
    if (activeAgent === "team") return; // composer hidden on team tab

    addChat("user", text);
    histories[activeAgent].push({role: "user", text});
    saveHistory(activeAgent);

    inputEl.value = "";
    autoGrow();

    // Direct shell command — route to terminal pane, not to agent
    if (text.startsWith("!")) {
      await runQuick(text.slice(1).trim());
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
      const res = await fetch("/agent/message", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          agent: activeAgent,
          text,
          workspace: {terminal_tail: getShellOutput()},
        })
      });
      const data = await res.json();
      const out = data.output || data.error || "(no response)";
      addChat("agent", out);
      histories[activeAgent].push({role: "agent", text: out});
      saveHistory(activeAgent);
    } catch(err) {
      const msg = "Web error: " + (err && err.message ? err.message : String(err));
      addChat("agent", msg);
      histories[activeAgent].push({role: "agent", text: msg});
      saveHistory(activeAgent);
    } finally {
      setBusy(false);
    }
  }

  async function runQuick(cmd) {
    const prompt = `jacks@${hostBadge.textContent || "host"}:$ ${cmd}`;
    termLine("prompt", prompt);

    setBusy(true);
    try {
      const res = await fetch("/message", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text: "!" + cmd})
      });
      const data = await res.json();
      const out = (data.stdout || "");
      const err = (data.stderr || data.error || "");
      if (out) termLine("out", out.trimEnd());
      if (err) termLine("err", err.trimEnd());
      if (!out && !err) termLine("out", "[no output]");
    } catch(err) {
      termLine("err", "SSH request failed: " + (err && err.message ? err.message : String(err)));
    } finally {
      setBusy(false);
    }
  }

  function confirmDockerRefresh() {
    if (!confirm("This will stop containers, hard-reset the repo, and rebuild the orchestrator. Continue?")) return;
    var s = [
      "bash <<'SH'",
      "set -euo pipefail",
      "",
      'REPO_DIR="/opt/openclaw-crypto"',
      'COMPOSE_FILE="docker-compose.orchestrator.yml"',
      'ENV_SRC="/etc/openclaw-crypto/openclaw.env"',
      "",
      'cd "$REPO_DIR"',
      "",
      'echo "== Stop containers (releases file locks) =="',
      'docker compose -f "$COMPOSE_FILE" down --remove-orphans || true',
      "",
      'echo "== Update repo to origin/main =="',
      "git fetch --all --prune",
      "git checkout main",
      "git reset --hard origin/main",
      "",
      'echo "== Fix permissions so git clean can remove container-created files =="',
      "if command -v sudo >/dev/null 2>&1; then",
      '  sudo chown -R "$(id -u):$(id -g)" . || true',
      "else",
      '  echo "WARNING: sudo not found."',
      "fi",
      "",
      'echo "== Clean untracked/ignored files =="',
      "git clean -fdx || { command -v sudo >/dev/null 2>&1 && sudo git clean -fdx; }",
      "",
      'echo "== Re-link env and start =="',
      'ln -sfn "$ENV_SRC" .env',
      "",
      'docker compose -f "$COMPOSE_FILE" up -d --build',
      'docker compose -f "$COMPOSE_FILE" ps',
      'docker compose -f "$COMPOSE_FILE" logs --tail=200',
      "SH"
    ].join("\\n");
    runQuick(s);
  }

  // Fetch SSH host label from server config
  let serverRepoDir = "";
  fetch("/config").then(r => r.json()).then(cfg => {
    if (cfg && cfg.ssh_host) hostBadge.textContent = cfg.ssh_host;
    if (cfg && cfg.repo_dir) serverRepoDir = cfg.repo_dir;
    if (cfg && Array.isArray(cfg.allowed_repos) && cfg.allowed_repos.length) {
      ALLOWED_REPOS.length = 0;
      cfg.allowed_repos.forEach(r => ALLOWED_REPOS.push(r));
    }
    updateRepoBadge();
  }).catch(() => {});

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

  function updateRepoBadge() {
    if (!repoBadgeEl) return;
    const sel = repoSelectEl ? repoSelectEl.value : "";
    if (sel && ALLOWED_REPOS.includes(sel)) {
      repoBadgeEl.textContent = "";
    } else {
      const detected = autoDetectRepo();
      const short = detected.split("/")[1] || detected;
      repoBadgeEl.textContent = "→ " + short;
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
      const res = await fetch("/copilot", {
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
        const url = "/copilot/poll/" + issueNumber + (repoParam ? "?repo=" + repoParam : "");
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

  // ── Team review ──────────────────────────────────────────────────────────────

  const TEAM_FEED_KEY = "openclaw_team_feed_v1";
  const AGENT_DISPLAY = {pnl: "P&L", quant: "Quant", coo: "COO", system: "System"};

  const quickReviewBtn    = document.getElementById("quickReviewBtn");
  const detailedReviewBtn = document.getElementById("detailedReviewBtn");
  const yearlyReviewBtn   = document.getElementById("yearlyReviewBtn");
  const cancelReviewBtn   = document.getElementById("cancelReviewBtn");

  let teamFeedEvents = [];
  let activeTeamRunId = null;
  let teamPollCursor = 0;
  let teamPollTimer = null;
  let teamRunCancelled = false;

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

    // Show content for message/error/run-start/cancelled events
    const showContent = ev.content && ["message","error","run-start","cancelled"].includes(ev.type);
    if (showContent) {
      const content = document.createElement("div");
      content.className = "feedContent";
      content.textContent = ev.content;
      row.appendChild(content);
    }

    return row;
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
  async function runTeamReview(mode, reviewPeriod) {
    if (activeTeamRunId) return; // already running

    teamRunCancelled = false;
    activeTeamRunId = null;
    teamPollCursor = 0;

    const period = reviewPeriod || "";
    const defaultPrompt = period
      ? `Produce a ${period} periodic review: (1) P&L summary with halt-state impact, ` +
        `(2) quant critique with halt trigger analysis, (3) COO recommendation. ` +
        `Clearly state if available data covers less than the requested period.`
      : "";
    const userPrompt = inputEl.value.trim() || defaultPrompt;
    const termTail   = getShellOutput();

    setTeamRunning(true);

    // Switch to the Team tab so the user sees the feed immediately
    activeAgent = "team";
    document.querySelectorAll(".tabBtn").forEach(b =>
      b.classList.toggle("active", b.getAttribute("data-agent") === "team"));
    showAgentTab("team");

    try {
      const res = await fetch("/team/review", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          mode,
          prompt: userPrompt,
          review_period: period,
          workspace: {terminal_tail: termTail},
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
      const res = await fetch(`/team/review/poll/${activeTeamRunId}?cursor=${teamPollCursor}`);
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

  quickReviewBtn.onclick    = () => runTeamReview("quick", "");
  detailedReviewBtn.onclick = () => runTeamReview("detailed", "");
  yearlyReviewBtn.onclick   = () => runTeamReview("detailed", "2-year");
  cancelReviewBtn.onclick   = cancelTeamReview;

  // ── Vibe execution gateway ────────────────────────────────────────────────

  const vibeGoalInputEl      = document.getElementById("vibeGoalInput");
  const vibeCommandInputEl   = document.getElementById("vibeCommandInput");
  const vibePlanBtnEl        = document.getElementById("vibePlanBtn");
  const vibeExecuteBtnEl     = document.getElementById("vibeExecuteBtn");
  const vibeApprovalBannerEl = document.getElementById("vibeApprovalBanner");
  const vibeApprovalCmdEl    = document.getElementById("vibeApprovalCmd");
  const vibeConfirmBtnEl     = document.getElementById("vibeConfirmBtn");
  const vibeCancelApprovalEl = document.getElementById("vibeCancelApprovalBtn");
  const vibeFeedEl           = document.getElementById("vibeFeed");

  let vibeRunId   = null;
  let vibePollTimer = null;
  let vibeSshHost = "";

  // Capture ssh_host from server config
  fetch("/config").then(r => r.json()).then(cfg => {
    if (cfg && cfg.ssh_host) vibeSshHost = cfg.ssh_host;
  }).catch(() => {});

  function vibeFeedAppend(text, kind) {
    const row = document.createElement("div");
    row.className = "vibeFeedRow " + (kind || "");
    row.textContent = text;
    vibeFeedEl.appendChild(row);
    vibeFeedEl.scrollTop = vibeFeedEl.scrollHeight;
  }

  function setVibeBusy(busy) {
    vibePlanBtnEl.disabled    = busy;
    vibeExecuteBtnEl.disabled = busy;
    vibeConfirmBtnEl.disabled = busy;
    setBusy(busy);
  }

  function showVibeApproval(command) {
    const prefix = vibeSshHost ? "ssh " + vibeSshHost + " " : "";
    vibeApprovalCmdEl.textContent = prefix + command;
    vibeApprovalBannerEl.style.display = "flex";
    vibeApprovalBannerEl.scrollIntoView({behavior: "smooth"});
  }

  function hideVibeApproval() {
    vibeApprovalBannerEl.style.display = "none";
  }

  // ✨ Plan with AI: ask the Vibe Planner agent to formulate a shell command
  vibePlanBtnEl.onclick = async () => {
    const goal = vibeGoalInputEl.value.trim();
    if (!goal) { vibeGoalInputEl.focus(); return; }
    hideVibeApproval();
    setVibeBusy(true);
    vibeFeedAppend("⏳ Planning with AI…", "info");
    try {
      const res = await fetch("/vibe/plan", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          goal,
          workspace: {terminal_tail: getShellOutput()},
        }),
      });
      const data = await res.json();
      const raw = data.output || data.error || "";
      try {
        // Agent should return JSON {"command": "..."}
        const parsed = JSON.parse(raw);
        if (parsed.command) vibeCommandInputEl.value = parsed.command;
        vibeFeedAppend("✅ AI plan ready — review command above, then execute.", "done");
      } catch {
        vibeFeedAppend("AI response (could not auto-fill — copy manually):\\n" + raw, "info");
      }
    } catch(err) {
      vibeFeedAppend("❌ Plan error: " + (err.message || String(err)), "err");
    } finally {
      setVibeBusy(false);
    }
  };

  // ▶ Approve & Execute: show approval banner
  vibeExecuteBtnEl.onclick = () => {
    const cmd = vibeCommandInputEl.value.trim();
    if (!cmd) { vibeCommandInputEl.focus(); return; }
    showVibeApproval(cmd);
  };

  // ✅ Confirm & Execute
  vibeConfirmBtnEl.onclick = async () => {
    const cmd = vibeCommandInputEl.value.trim();
    if (!cmd) return;
    hideVibeApproval();
    setVibeBusy(true);
    vibeFeedAppend("🚀 Dispatching Vibe…", "info");
    vibeFeedAppend("command: " + cmd, "info");
    try {
      const res = await fetch("/vibe/execute", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({command: cmd}),
      });
      const data = await res.json();
      if (data.error) {
        vibeFeedAppend("❌ " + data.error, "err");
        setVibeBusy(false);
        return;
      }
      vibeRunId = data.run_id;
      vibeFeedAppend("⏳ Run ID: " + vibeRunId + " — executing…", "info");
      vibePollTimer = setInterval(pollVibeRun, 3000);
    } catch(err) {
      vibeFeedAppend("❌ Request error: " + (err.message || String(err)), "err");
      setVibeBusy(false);
    }
  };

  // ✗ Cancel approval
  vibeCancelApprovalEl.onclick = hideVibeApproval;

  async function pollVibeRun() {
    if (!vibeRunId) return;
    try {
      const res = await fetch("/vibe/poll/" + vibeRunId);
      const data = await res.json();
      if (data.status === "done") {
        clearInterval(vibePollTimer);
        vibePollTimer = null;
        vibeRunId = null;
        vibeFeedAppend("✅ Vibe finished:\\n" + (data.output || "(no output)"), "done");
        setVibeBusy(false);
      } else if (data.status === "error") {
        clearInterval(vibePollTimer);
        vibePollTimer = null;
        vibeRunId = null;
        vibeFeedAppend("❌ Vibe error: " + (data.error || "unknown error"), "err");
        setVibeBusy(false);
      } else if (data.status === "not_found") {
        clearInterval(vibePollTimer);
        vibePollTimer = null;
        vibeRunId = null;
        vibeFeedAppend("❌ Run not found.", "err");
        setVibeBusy(false);
      }
      // "running" → keep polling
    } catch(_err) {
      // Network blip — keep polling
    }
  }
</script>
</body>
</html>
"""


def _try_assign_copilot(owner: str, repo: str, issue_number: int, token: str) -> str:
    """Attempt to assign Copilot to an issue. Returns 'assigned' or 'manual_required'."""
    headers = _gh_headers(token)
    # Discover copilot-like login from repo assignees
    copilot_login = None
    try:
        r = _requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/assignees",
            headers=headers,
            timeout=10,
        )
        if r.ok:
            for user in r.json():
                login = user.get("login", "")
                if "copilot" in login.lower():
                    copilot_login = login
                    break
    except Exception:
        pass

    if not copilot_login:
        return "manual_required"

    try:
        r = _requests.post(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/assignees",
            json={"assignees": [copilot_login]},
            headers=headers,
            timeout=10,
        )
        if r.ok:
            return "assigned"
    except Exception:
        pass

    return "manual_required"


@app.post("/copilot")
def copilot_issue(req: CopilotRequest):
    token = settings.github_token
    if not token:
        return {"error": "GITHUB_TOKEN is not configured. Set the GITHUB_TOKEN environment variable."}

    # Resolve target repo: client-supplied value wins if it is in the allowed list.
    if req.target_repo and req.target_repo in _ALLOWED_REPOS:
        repo_full = req.target_repo
    else:
        repo_full = settings.github_repo

    parts = repo_full.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return {"error": "Target repo must be in 'owner/repo' format"}

    owner, repo = parts
    title_text = req.goal.strip()[:80] if req.goal.strip() else "Task from OpenClaw UI"
    title = f"[Copilot] {title_text}"
    body = _build_issue_body(req)

    url = f"https://api.github.com/repos/{owner}/{repo}/issues"
    payload = {
        "title": title,
        "body": body,
        "labels": ["copilot"],
    }

    try:
        r = _requests.post(url, json=payload, headers=_gh_headers(token), timeout=15)
        if not r.ok:
            return {"error": f"GitHub API error {r.status_code}: {r.text[:300]}"}
        data = r.json()
        issue_number = data["number"]
        issue_url = data["html_url"]
    except _requests.exceptions.RequestException as e:
        return {"error": f"GitHub API request failed: {type(e).__name__}"}
    except Exception:
        return {"error": "Unexpected error creating issue. Check server logs."}

    # Attempt to assign Copilot to the issue
    assignment = _try_assign_copilot(owner, repo, issue_number, token)
    return {
        "issue_url": issue_url,
        "issue_number": issue_number,
        "assignment": assignment,
        "used_repo": repo_full,
    }


@app.get("/copilot/poll/{issue_number}")
def copilot_poll(issue_number: int, repo: str = ""):
    token = settings.github_token
    if not token:
        return {"pr_url": None}

    # Use client-supplied repo if it is in the allowed list, else fall back to server default.
    repo_full = repo if (repo and repo in _ALLOWED_REPOS) else settings.github_repo
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


# ── Vibe execution gateway endpoints ─────────────────────────────────────────

@app.post("/vibe/plan")
def vibe_plan(req: VibePlanRequest):
    """Ask the Vibe Planner agent to formulate a shell command for a given goal."""
    return handle_agent_message("vibe", req.goal, req.workspace)


@app.post("/vibe/execute")
def vibe_execute(req: VibeExecuteRequest):
    """Start a shell command run via SSH after user approval. Returns run_id for polling."""
    command = (req.command or "").strip()
    if not command:
        return {"error": "command is required"}
    run_id = start_vibe_run(command)
    return {"run_id": run_id}


@app.get("/vibe/poll/{run_id}")
def vibe_poll(run_id: str):
    """Return the current status and output of a Vibe run."""
    return get_vibe_run(run_id)