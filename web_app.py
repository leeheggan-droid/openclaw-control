import requests as _requests

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from openclaw_control.config import settings
from openclaw_control.service import handle_message

app = FastAPI()


class Message(BaseModel):
    text: str


class CopilotRequest(BaseModel):
    goal: str
    last_user_msg: str = ""
    last_agent_response: str = ""
    shell_output: str = ""


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
    return {"ssh_host": settings.ssh_host}


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
  </style>
</head>

<body>
  <div class="app">
    <!-- LEFT -->
    <section class="card" id="leftCard">
      <div class="cardHeader">
        <div class="title">
          <span style="width:10px;height:10px;border-radius:999px;background:var(--accent);display:inline-block"></span>
          Agent
          <span class="badge" id="agentMode">ops</span>
        </div>
        <div class="badge" id="statusBadge">ready</div>
      </div>

      <div class="chatBody" id="chat"></div>

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
          <div style="display:flex;gap:12px;align-items:center;">
            <span>Shell: <code>!uptime</code>, <code>!docker ps</code></span>
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
  // --- state ---
  const chatEl = document.getElementById("chat");
  const terminalEl = document.getElementById("terminal");
  const inputEl = document.getElementById("input");
  const sendBtn = document.getElementById("sendBtn");
  const statusBadge = document.getElementById("statusBadge");
  const agentModeEl = document.getElementById("agentMode");
  const hostBadge = document.getElementById("hostBadge");

  const fileInput = document.getElementById("fileInput");
  const imgInput = document.getElementById("imgInput");
  document.getElementById("attachBtn").onclick = () => fileInput.click();
  document.getElementById("imageBtn").onclick = () => imgInput.click();

  // simple local history (persists across refresh)
  const storeKey = "openclaw_chat_history_v1";
  let history = [];
  try { history = JSON.parse(localStorage.getItem(storeKey) || "[]"); } catch { history = []; }

  function saveHistory(){
    localStorage.setItem(storeKey, JSON.stringify(history.slice(-200)));
  }

  function scrollChatBottom(){
    chatEl.scrollTop = chatEl.scrollHeight;
  }
  function scrollTermBottom(){
    terminalEl.scrollTop = terminalEl.scrollHeight;
  }

  function addChat(role, text, extraHTML){
    const row = document.createElement("div");
    row.className = "msgRow " + (role === "user" ? "user" : "agent");

    const wrap = document.createElement("div");

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = role === "user" ? "You" : "Agent";
    wrap.appendChild(meta);

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text || "";
    if(extraHTML){
      const holder = document.createElement("div");
      holder.innerHTML = extraHTML;
      bubble.appendChild(holder);
    }
    wrap.appendChild(bubble);

    // Add "Fix via Copilot" button below each agent message (skip status messages)
    if(role === "agent" && text && !/^[⏳✅🚀❌]/.test(text)){
      const capturedText = text;
      const copBtn = document.createElement("button");
      copBtn.className = "copilotMsgBtn";
      copBtn.textContent = "🤖 Fix via Copilot";
      copBtn.onclick = () => {
        const lastUser = [...history].reverse().find(h => h.role === "user");
        const goal = prompt("Describe the goal for Copilot:", (lastUser ? lastUser.text : "").slice(0, 120)) || "";
        if(!goal) return;
        triggerCopilot(goal, lastUser ? lastUser.text : "", capturedText);
      };
      wrap.appendChild(copBtn);
    }

    row.appendChild(wrap);
    chatEl.appendChild(row);
    scrollChatBottom();
  }

  function renderHistory(){
    chatEl.innerHTML = "";
    history.forEach(item => addChat(item.role, item.text, item.extraHTML || ""));
  }
  renderHistory();

  function termLine(kind, text){
    const div = document.createElement("div");
    div.className = "termLine " + kind;
    div.textContent = text;
    terminalEl.appendChild(div);
    scrollTermBottom();
  }

  function clearTerminal(){
    terminalEl.innerHTML = "";
  }

  function setBusy(isBusy){
    statusBadge.textContent = isBusy ? "thinking…" : "ready";
    statusBadge.style.borderColor = isBusy ? "rgba(34,197,94,.55)" : "rgba(255,255,255,.08)";
    statusBadge.style.color = isBusy ? "rgba(230,238,252,.85)" : "rgba(230,238,252,.65)";
  }

  // Auto-grow textarea
  function autoGrow(){
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 130) + "px";
  }
  inputEl.addEventListener("input", autoGrow);
  autoGrow();

  // Send on Enter, newline on Shift+Enter
  inputEl.addEventListener("keydown", (e) => {
    if(e.key === "Enter" && !e.shiftKey){
      e.preventDefault();
      send();
    }
  });
  sendBtn.onclick = () => send();

  // Attach file (text)
  fileInput.onchange = async (e) => {
    const f = e.target.files && e.target.files[0];
    if(!f) return;
    // Read small text files into the prompt
    const maxBytes = 200 * 1024;
    if(f.size > maxBytes){
      addChat("user", `(attached file too large for inline text: ${f.name}, ${f.size} bytes)`);
      history.push({role:"user", text:`(attached file too large for inline text: ${f.name})`});
      saveHistory();
      fileInput.value = "";
      return;
    }
    const text = await f.text();
    const extra = `<div class="attachment">FILE: ${escapeHtml(f.name)}\\n\\n${escapeHtml(text)}</div>`;
    addChat("user", `Attached: ${f.name}`, extra);
    history.push({role:"user", text:`Attached: ${f.name}`, extraHTML: extra});
    saveHistory();
    fileInput.value = "";
  };

  // Attach image (preview only in v1)
  imgInput.onchange = async (e) => {
    const f = e.target.files && e.target.files[0];
    if(!f) return;
    const url = URL.createObjectURL(f);
    const extra = `<div class="imgPreview"><img src="${url}" alt="attachment"/></div>`;
    addChat("user", `Attached image: ${f.name}`, extra);
    history.push({role:"user", text:`Attached image: ${f.name}`, extraHTML: extra});
    saveHistory();
    imgInput.value = "";
  };

  function escapeHtml(s){
    return (s || "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");
  }

  async function send(textOverride){
    const text = (textOverride !== undefined) ? textOverride : inputEl.value.trim();
    if(!text) return;

    // show message in history
    addChat("user", text);
    history.push({role:"user", text});
    saveHistory();

    inputEl.value = "";
    autoGrow();

    // If direct command, run in terminal pane
    if(text.startsWith("!")){
      await runQuick(text.slice(1).trim());
      return;
    }

    // Handle /copilot and "fix via copilot:" commands
    const lc = text.toLowerCase();
    if(lc.startsWith("/copilot") || lc.startsWith("fix via copilot:")){
      let goal;
      if(lc.startsWith("/copilot")){
        goal = text.slice("/copilot".length).trim();
      } else {
        goal = text.slice("fix via copilot:".length).trim();
      }
      const prevHistory = history.slice(0, -1);
      const lastUser = [...prevHistory].reverse().find(h => h.role === "user");
      const lastAgent = [...prevHistory].reverse().find(h => h.role === "agent");
      if(!goal) goal = prompt("Describe the goal for Copilot:", lastUser ? lastUser.text.slice(0,120) : "") || "";
      if(goal) await triggerCopilot(goal, lastUser ? lastUser.text : "", lastAgent ? lastAgent.text : "");
      return;
    }

    setBusy(true);
    agentModeEl.textContent = "ops/analysis";

    try{
      const res = await fetch("/message", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({text})
      });
      const data = await res.json();

      if(data.type === "agent"){
        const out = data.output || data.error || "(no response)";
        addChat("agent", out);
        history.push({role:"agent", text: out});
        saveHistory();
      } else if(data.type === "ssh"){
        const out = (data.stdout || "") + (data.stderr || "") + (data.error || "");
        termLine("out", out || "[no output]");
      }
    } catch(err){
      const msg = "Web error: " + (err && err.message ? err.message : String(err));
      addChat("agent", msg);
      history.push({role:"agent", text: msg});
      saveHistory();
    } finally{
      setBusy(false);
    }
  }

  async function runQuick(cmd){
    // terminal prompt line
    const prompt = `jacks@${hostBadge.textContent || "host"}:$ ${cmd}`;
    termLine("prompt", prompt);

    setBusy(true);
    try{
      const res = await fetch("/message", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({text: "!" + cmd})
      });
      const data = await res.json();
      const out = (data.stdout || "");
      const err = (data.stderr || data.error || "");
      if(out) termLine("out", out.trimEnd());
      if(err) termLine("err", err.trimEnd());
      if(!out && !err) termLine("out", "[no output]");
    } catch(err){
      termLine("err", "SSH request failed: " + (err && err.message ? err.message : String(err)));
    } finally{
      setBusy(false);
    }
  }

  function confirmDockerRefresh(){
    if(!confirm("This will stop containers, hard-reset the repo, and rebuild the orchestrator. Continue?")) return;
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

  // fetch SSH host label from server config
  fetch("/config").then(r => r.json()).then(cfg => {
    if(cfg && cfg.ssh_host) hostBadge.textContent = cfg.ssh_host;
  }).catch(() => {});

  // --- Copilot bridge ---

  function getShellOutput(){
    return Array.from(terminalEl.querySelectorAll(".termLine"))
      .map(el => el.textContent)
      .slice(-200)
      .join("\\n");
  }

  async function triggerCopilot(goal, lastUserMsg, lastAgentMsg){
    if(!goal) return;
    const shellOutput = getShellOutput();

    const statusMsg = "⏳ Creating Copilot issue on GitHub…";
    addChat("agent", statusMsg);
    history.push({role:"agent", text: statusMsg});
    saveHistory();

    let data;
    try{
      const res = await fetch("/copilot", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          goal,
          last_user_msg: lastUserMsg || "",
          last_agent_response: lastAgentMsg || "",
          shell_output: shellOutput,
        })
      });
      data = await res.json();
    } catch(err){
      const msg = "❌ Copilot error: " + (err && err.message ? err.message : String(err));
      addChat("agent", msg);
      history.push({role:"agent", text: msg});
      saveHistory();
      return;
    }

    if(data.error){
      const msg = "❌ Copilot issue failed: " + data.error;
      addChat("agent", msg);
      history.push({role:"agent", text: msg});
      saveHistory();
      return;
    }

    const issueUrl = data.issue_url;
    const issueNum = data.issue_number;
    let msg = "✅ Copilot issue #" + issueNum + " created:\\n" + issueUrl;
    if(data.assignment === "manual_required"){
      msg += "\\n\\n⚠️ Issue created. Assign Copilot manually in GitHub (Assignees → Copilot).";
    } else {
      msg += "\\n\\nMonitoring for PR…";
    }
    addChat("agent", msg);
    history.push({role:"agent", text: msg});
    saveHistory();

    pollForPR(issueNum);
  }

  function pollForPR(issueNumber){
    let attempts = 0;
    const maxAttempts = 20; // 20 × 15s = 5 min
    const timer = setInterval(async () => {
      attempts++;
      if(attempts > maxAttempts){
        clearInterval(timer);
        return;
      }
      try{
        const res = await fetch("/copilot/poll/" + issueNumber);
        const data = await res.json();
        if(data.pr_url){
          clearInterval(timer);
          const msg = "🚀 Copilot PR created:\\n" + data.pr_url;
          addChat("agent", msg);
          history.push({role:"agent", text: msg});
          saveHistory();
        }
      } catch{}
    }, 15000);
  }

  document.getElementById("copilotBtn").onclick = async () => {
    const lastUser = [...history].reverse().find(h => h.role === "user");
    const lastAgent = [...history].reverse().find(h => h.role === "agent");
    const def = lastUser ? lastUser.text.slice(0, 120) : "";
    const goal = prompt("Describe the goal for Copilot:", def) || "";
    if(!goal) return;
    await triggerCopilot(goal, lastUser ? lastUser.text : "", lastAgent ? lastAgent.text : "");
  };
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

    parts = settings.github_repo.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return {"error": "GITHUB_REPO must be in 'owner/repo' format"}

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
    }


@app.get("/copilot/poll/{issue_number}")
def copilot_poll(issue_number: int):
    token = settings.github_token
    if not token:
        return {"pr_url": None}

    parts = settings.github_repo.split("/", 1)
    if len(parts) != 2:
        return {"pr_url": None}

    owner, repo = parts
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/timeline"

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