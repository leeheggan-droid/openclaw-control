from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from openclaw_control.service import handle_message

app = FastAPI()


class Message(BaseModel):
    text: str


@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!DOCTYPE html>
<html>
<head>
  <title>OpenClaw Control</title>
  <style>
    body { font-family: sans-serif; margin: 0; }
    .container { display: grid; grid-template-columns: 1fr 1fr; height: 100vh; }
    .pane { padding: 12px; border-right: 1px solid #ccc; overflow-y: auto; }
    .right { border-right: none; background: #fafafa; }
    textarea { width: 100%; }
    button { margin-right: 6px; }
    pre { white-space: pre-wrap; }
  </style>
</head>
<body>
<div class="container">
  <div class="pane">
    <h2>Agent</h2>
    <textarea id="input" rows="4"></textarea><br/>
    <button onclick="send()">Send</button>
    <pre id="agent_out"></pre>
  </div>
  <div class="pane right">
    <h2>Command Output</h2>
    <button onclick="quick('!uptime')">uptime</button>
    <button onclick="quick('!docker ps')">docker ps</button>
    <pre id="cmd_out"></pre>
  </div>
</div>

<script>
async function send(textOverride) {
  const text = textOverride || document.getElementById("input").value;
  const res = await fetch("/message", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({text})
  });
  const data = await res.json();

  if (data.type === "agent") {
    document.getElementById("agent_out").textContent =
      data.output || data.error;
  } else if (data.type === "ssh") {
    document.getElementById("cmd_out").textContent =
      (data.stdout || "") + (data.stderr || "") + (data.error || "");
  }
}

function quick(cmd) {
  send(cmd);
}
</script>
</body>
</html>
"""


@app.post("/message")
def message(msg: Message):
    return handle_message(msg.text)