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
- SSH connections use `BatchMode=yes` (no interactive prompts) and `StrictHostKeyChecking=yes` (no TOFU/silent MITM), enforcing that the VPS host key must already be in `~/.ssh/known_hosts` on the control plane host.
- The subprocess timeout is 900 seconds. Long-running Vibe invocations block one daemon thread per run; in-flight runs survive a server restart only if the SSH connection is still alive.
- Operator authentication (e.g. reverse-proxy basic auth) is outside the scope of this component but is strongly recommended.

---

## VPS-Side Setup (Forced-Command Wrapper)

To lock the dedicated SSH key so it can **only** run `vibe` (no interactive shell, no other commands):

### 1 — Generate a dedicated key on the control plane

```bash
ssh-keygen -t ed25519 -C "openclaw-vibe" -f ~/.ssh/openclaw_vibe_ed25519 -N ""
```

### 2 — Add the key to `~/.ssh/authorized_keys` on the VPS

The simplest approach is to use `restrict` (OpenSSH 7.4+) with `command="vibe"`, which prevents
any interactive shell while still allowing the control plane to pass `--workdir` and `--prompt`
as arguments on the SSH command line:

```
restrict,command="vibe" ssh-ed25519 AAAA… openclaw-vibe
```

With this entry, `ssh <host> "vibe --workdir /opt/repo --prompt 'fix thing'"` executes correctly,
but `ssh <host>` (no command) is rejected by the forced-command wrapper.

To restrict the workdir to a specific subtree you can wrap `vibe` in a small shell script on the
VPS (e.g. `/usr/local/bin/openclaw-vibe`) that validates the `--workdir` argument before
delegating to `vibe`, and reference that script in `command=` instead.

### 3 — Register the VPS host key on the control plane

```bash
ssh-keyscan -H <VPS_IP_OR_HOSTNAME> >> ~/.ssh/known_hosts
```

`StrictHostKeyChecking=yes` will reject the connection if the host key is not already trusted.

### 4 — Configure the control plane

Add to `.env`:

```
OPENCLAW_SSH_HOST=<user>@<vps-ip-or-hostname>
OPENCLAW_VIBE_WORKDIR=/opt/openclaw-crypto
```

Optionally specify the key explicitly in `~/.ssh/config`:

```
Host <vps-ip-or-hostname>
    IdentityFile ~/.ssh/openclaw_vibe_ed25519
    IdentitiesOnly yes
```

### 5 — Smoke-test

```bash
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes \
    <user>@<vps-ip-or-hostname> \
    "vibe --workdir /opt/openclaw-crypto --prompt 'echo hello'"
```

Expected: exit 0, vibe output in stdout.

---

## Common Failure Modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `Host key verification failed` | Host key not in `known_hosts` | Run `ssh-keyscan` (step 3 above) |
| `Permission denied (publickey)` | Wrong key, key not in `authorized_keys` | Check `~/.ssh/authorized_keys` on VPS; verify key fingerprint |
| `command not found: vibe` | `vibe` not on `PATH` for the SSH user | Install vibe or use an absolute path in the `authorized_keys` `command=` |
| `SSH error (TimeoutExpired)` | VPS unreachable or firewall blocking port 22 | Check VPS firewall rules; verify `OPENCLAW_SSH_HOST` |
| `OPENCLAW_SSH_HOST is not configured` | `.env` missing the variable | Set `OPENCLAW_SSH_HOST=user@host` in `.env` |

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
