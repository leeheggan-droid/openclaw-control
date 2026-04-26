# Vibe Execution Gateway

## Overview

The Vibe Execution Gateway integrates the [Vibe](https://github.com/anthropics/vibe) AI coding tool as a **permissioned execution channel** inside the OpenClaw multi-agent cockpit. It gives operators a single, auditable path to request code changes on the live server — with mandatory human review before any `vibe` command is sent to the VPS over SSH.

---

## Architecture

```
Browser (Vibe tab)
   │
   ├─ POST /vibe/plan          ──► Vibe Planner Agent (no tools, read-only)
   │    └─ Returns JSON: {workdir, prompt}
   │
   ├─ POST /vibe/execute       ──► start_vibe_run() — background thread
   │    └─ Returns {run_id}         └─ ssh <OPENCLAW_SSH_HOST> vibe --workdir … --prompt …
   │
   └─ GET  /vibe/poll/{run_id} ──► get_vibe_run() — status + stdout/stderr
```

### Components

| Layer | File | Role |
|---|---|---|
| Agent | `openclaw_control/agents/vibe_agent.py` | Formulates `{workdir, prompt}` JSON from a goal; no tool execution |
| Tool (reference) | `openclaw_control/tools/vibe_tools.py` | `vibe_prompt` decorated with `needs_approval=True`; documents the approval intent |
| Service | `openclaw_control/service.py` | `start_vibe_run` / `get_vibe_run`; long-running SSH subprocess in daemon thread |
| API | `web_app.py` | `/vibe/plan`, `/vibe/execute`, `/vibe/poll/{run_id}` |
| UI | `web_app.py` (inline HTML/JS) | Vibe tab with Plan → Approve → Execute → Poll flow |
| Config | `openclaw_control/config.py` | `OPENCLAW_VIBE_WORKDIR` — default working directory; `OPENCLAW_SSH_HOST` — VPS target |

---

## Permission Model

Execution of the `vibe` CLI on the VPS is **always gated by an explicit human approval step**:

1. **Plan** — User describes their goal. The Vibe Planner agent (LLM, no tools) returns a structured `{workdir, prompt}` JSON that populates the UI fields. The user can edit either field freely.
2. **Approve** — Clicking **▶ Approve & Execute** surfaces a yellow warning banner showing the exact SSH command that will run:
   ```
   ssh <host> vibe --workdir /opt/openclaw-crypto --prompt "<prompt text>"
   ```
3. **Confirm** — Only after clicking **✅ Confirm & Execute** does the backend open the SSH connection and run `vibe` on the VPS.

No API call to `/vibe/execute` is made until step 3. The browser never skips the approval banner.

> The `vibe_prompt` function tool (`vibe_tools.py`) carries `needs_approval=True` as an additional safety annotation should the tool ever be wired into an automated agent loop.

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `OPENCLAW_SSH_HOST` | `""` | VPS SSH target used to run the `vibe` command remotely. |
| `OPENCLAW_VIBE_WORKDIR` | `""` | Default workdir pre-filled in the UI. Falls back to `OPENCLAW_REPO_DIR` if empty. |

---

## API Reference

### `POST /vibe/plan`

Ask the Vibe Planner agent to formulate a workdir + prompt for a given goal.

**Request body:**
```json
{
  "goal": "Add a stop-loss guard to the orchestrator",
  "workspace": {"terminal_tail": "…optional last 200 lines of shell output…"}
}
```

**Response:**
```json
{
  "agent": "vibe",
  "output": "{\"workdir\": \"/opt/openclaw-crypto\", \"prompt\": \"Add a configurable stop-loss guard…\"}"
}
```

---

### `POST /vibe/execute`

Start a Vibe run (via SSH) after the user has approved the workdir and prompt.

**Request body:**
```json
{
  "workdir": "/opt/openclaw-crypto",
  "prompt": "Add a configurable stop-loss guard to the orchestrator"
}
```

**Response:**
```json
{"run_id": "a1b2c3d4"}
```

---

### `GET /vibe/poll/{run_id}`

Poll the status of a running or completed Vibe job.

**Response (running):**
```json
{"status": "running", "output": "", "error": ""}
```

**Response (done):**
```json
{
  "status": "done",
  "output": "exit=0\nSTDOUT:\n…vibe output…\nSTDERR:\n",
  "error": ""
}
```

**Response (error):**
```json
{"status": "error", "output": "", "error": "Vibe timed out after 900 seconds."}
```

---

## UI Flow

```
 ┌─ Vibe tab active ──────────────────────────────────────────────────────┐
 │                                                                        │
 │  Workdir  [ /opt/openclaw-crypto                                     ] │
 │  Prompt   [ Add stop-loss guard …                                    ] │
 │                                                                        │
 │  [✨ Plan with AI]   [▶ Approve & Execute]                             │
 │                                                                        │
 │  ┌─ ⚠️ Review & Confirm Vibe Execution ───────────────────────────┐   │
 │  │  ssh jacks@72.61.123.4 vibe --workdir /opt/… --prompt "…"     │   │
 │  │  [✅ Confirm & Execute]   [✗ Cancel]                           │   │
 │  └────────────────────────────────────────────────────────────────┘   │
 │                                                                        │
 │  feed:                                                                 │
 │  🚀 Dispatching Vibe…                                                  │
 │  workdir: /opt/openclaw-crypto                                         │
 │  prompt:  Add stop-loss guard …                                        │
 │  ⏳ Run ID: a1b2c3d4 — executing (may take up to 15 min)…              │
 │  ✅ Vibe finished:                                                     │
 │  exit=0                                                                │
 │  STDOUT: …                                                             │
 └────────────────────────────────────────────────────────────────────────┘
```

---

## Security Considerations

- The `/vibe/execute` endpoint accepts arbitrary `workdir` and `prompt` values. Deploy OpenClaw Control in a **trusted internal network** only.
- The `workdir` and `prompt` values are shell-quoted via `shlex.quote` before being passed as the remote SSH command string, preventing shell injection on the VPS.
- The subprocess timeout is 900 seconds. Long-running Vibe invocations block one daemon thread per run; in-flight runs survive a server restart only if the SSH connection is still alive.
- Operator authentication (e.g. reverse-proxy basic auth) is outside the scope of this component but is strongly recommended.

---

## Local Test Checklist

- [ ] `git pull`
- [ ] `uvicorn web_app:app --reload`
- [ ] Open browser → navigate to the cockpit
- [ ] Click the **Vibe** tab — the pad with workdir/prompt fields is visible
- [ ] Click **✨ Plan with AI** — feed shows AI-generated workdir + prompt, fields are pre-filled
- [ ] Click **▶ Approve & Execute** — yellow approval banner appears with SSH command (e.g. `ssh jacks@72.61.123.4 vibe --workdir … --prompt …`)
- [ ] Click **✗ Cancel** — banner disappears, no execution occurs
- [ ] Repeat approve → **✅ Confirm & Execute** — feed shows `🚀 Dispatching Vibe…` and polls for output
