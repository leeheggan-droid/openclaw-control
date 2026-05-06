# How Link Interacts With openclaw-control

> This file documents the complete end-to-end flow by which Link (the Vercel AI
> assistant at www.leeheggan.tech) triggers operational actions on the VPS via
> this repository.

---

## Overview

Link uses the **direct VPS Control API** (HTTP on port 8765) as the primary
control path.  This gives sub-second responses without waiting 30–90 s for a
GitHub Actions runner.

Link does **not** SSH directly into the VPS.  If the VPS Control API is
unreachable, Link falls back to triggering a `workflow_dispatch` event on
`link.yml` via the GitHub API, which then SSHes in through an Ansible runner.

See `link/context/environment.md` for the currently active context.

---

## Primary Path — Direct VPS Control API

```
┌──────────────────────────────────────────────┐
│  Link (Vercel / www.leeheggan.tech)          │
│                                              │
│  User says: "is the agent running?"          │
│      ↓                                       │
│  Read VPS_CONTROL_API_URL and                │
│  VPS_CONTROL_API_KEY from Vercel env vars    │
│      ↓                                       │
│  GET http://72.61.123.4:8765/                │
│       status/openclaw-agent.service          │
│  Authorization: Bearer <VPS_CONTROL_API_KEY> │
└───────────────────┬──────────────────────────┘
                    │  HTTP (direct, no intermediary)
                    ▼
┌──────────────────────────────────────────────┐
│  VPS — srv1501082 / 72.61.123.4              │
│                                              │
│  openclaw-control-api.service (port 8765)    │
│  validates API key, runs:                    │
│  systemctl is-active openclaw-agent.service  │
│      ↓                                       │
│  Returns JSON immediately                    │
└───────────────────┬──────────────────────────┘
                    │  JSON response (< 1 second)
                    ▼
        { "service": "openclaw-agent.service",
          "active": true, "state": "active" }
```

### Vercel environment variables required

| Variable              | Description                                      |
|-----------------------|--------------------------------------------------|
| `VPS_CONTROL_API_URL` | `http://72.61.123.4:8765`                        |
| `VPS_CONTROL_API_KEY` | Shared secret matching `/etc/openclaw-control-api.env` on the VPS |

### Endpoints

| Action needed by Link                  | Method | Path |
|----------------------------------------|--------|------|
| Liveness probe                         | `GET`  | `/health` |
| Service status                         | `GET`  | `/status/{service}` |
| Recent logs                            | `GET`  | `/logs/{service}?n=<lines>` |
| Restart service                        | `POST` | `/restart/{service}` |
| Deploy service (git pull + restart)    | `POST` | `/deploy/{service}` |

See `link/context/vps-control-api.md` for allowed service names, full response
shapes, and example calls.

---

## Fallback Path — GitHub Actions

Use this path **only** if the VPS Control API is unreachable (e.g. service
crashed, VPS firewall change, or API key mismatch).

```
┌──────────────────────────────────────────────┐
│  Link (Vercel / www.leeheggan.tech)          │
│                                              │
│  VPS Control API unreachable                 │
│  (connection refused / timeout / 401)        │
│      ↓                                       │
│  Link calls GitHub API                       │
│  POST /repos/leeheggan-droid/                │
│    openclaw-control/actions/workflows/       │
│    link.yml/dispatches                       │
│  { ref: "main",                              │
│    inputs: { action: "systemd-restart" } }   │
└───────────────────┬──────────────────────────┘
                    │  HTTPS (GitHub API token)
                    ▼
┌──────────────────────────────────────────────┐
│  GitHub Actions — ubuntu-latest runner       │
│                                              │
│  1. Checkout repo                            │
│  2. pip install ansible                      │
│  3. Write VPS_SSH_KEY → ~/.ssh/id_rsa        │
│  4. Write ANSIBLE_INVENTORY → inventory.ini  │
│  5. ansible-playbook site.yml                │
│        --tags "systemd-restart"              │
└───────────────────┬──────────────────────────┘
                    │  SSH (RSA key from VPS_SSH_KEY secret)
                    ▼
┌──────────────────────────────────────────────┐
│  VPS — srv1501082 / 72.61.123.4              │
│                                              │
│  Ansible runs systemctl / journalctl         │
└───────────────────┬──────────────────────────┘
                    │
                    ▼
        Results → GitHub Actions run log
        (Link reads log via GitHub API if needed)
```

### When to use the fallback

| Symptom from VPS API                  | Likely cause                        | Action                                      |
|---------------------------------------|-------------------------------------|---------------------------------------------|
| `Connection refused` / timeout        | `openclaw-control-api.service` down | Use GitHub Actions fallback; alert operator |
| `401 Unauthorized`                    | Key mismatch                        | Operator must rotate key; use fallback      |
| `400 Bad Request`                     | Service name not in allow-list      | Fix service name — no fallback needed       |
| `500 Internal Server Error`           | systemctl/journalctl failed on VPS  | Check VPS directly; optionally use fallback |

> When Link falls back to GitHub Actions it must **tell the user** that the
> direct API was unreachable and explain that the response will be delayed
> (30–90 s runner spin-up).

---

## GitHub Actions — workflow_dispatch inputs

The workflow input is named **`action`** (not `task`).

| Input        | Type   | Required | Values                                                                                                       |
|--------------|--------|----------|--------------------------------------------------------------------------------------------------------------|
| `action`     | choice | **Yes**  | `status-all`, `systemd-status`, `systemd-logs`, `systemd-restart`, `systemd-stop`, `systemd-start`, `logs-systemd` |
| `service`    | string | No       | Exact unit name — **required** for `systemd-stop`, `systemd-start`, `logs-systemd` (default: `""`)         |
| `tail_lines` | string | No       | Number of log lines — applies to `systemd-logs` and `logs-systemd` (default: `50`)                          |

**Example — restart all systemd bots:**
```
POST https://api.github.com/repos/leeheggan-droid/openclaw-control/actions/workflows/link.yml/dispatches
Authorization: Bearer <GITHUB_TOKEN>
Content-Type: application/json

{
  "ref": "main",
  "inputs": {
    "action": "systemd-restart"
  }
}
```

**Example — fetch logs from a specific service (`logs-systemd`):**
```
POST …/dispatches
{
  "ref": "main",
  "inputs": {
    "action": "logs-systemd",
    "service": "openclaw-crypto.service",
    "tail_lines": "30"
  }
}
```

> The API returns **`HTTP 204 No Content`** immediately — this means *accepted*,
> not *completed*.  The workflow run starts asynchronously.  Link must poll the
> GitHub API to know whether it succeeded.

### Reading GitHub Actions results

**Step 1 — get the latest run ID:**
```
GET /repos/leeheggan-droid/openclaw-control/actions/runs?event=workflow_dispatch&per_page=1
```
Parse `workflow_runs[0].id` from the response.

**Step 2 — poll until the run finishes:**
```
GET /repos/leeheggan-droid/openclaw-control/actions/runs/{run_id}
```
Repeat every ~10 seconds until `status == "completed"`.
Then check `conclusion`:
- `"success"` — playbook ran without errors
- `"failure"` — playbook failed (SSH error, Ansible error, etc.)
- `"cancelled"` / `"timed_out"` — runner issue

**Step 3 — fetch the log output:**
```
GET /repos/leeheggan-droid/openclaw-control/actions/runs/{run_id}/logs
```
Returns a ZIP archive containing the Ansible output.

### GitHub token requirements for fallback

| Token type            | Required permission / scope                              |
|-----------------------|----------------------------------------------------------|
| Fine-grained PAT      | **Actions: Read and Write** on `leeheggan-droid/openclaw-control` |
| Classic PAT           | **`workflow`** scope                                     |

> See `link/context/github-token.md` for a step-by-step verification guide.

---

## Verifying API Connectivity

Run the **"Verify VPS Control API"** workflow (`verify-vps-api.yml`) from GitHub
Actions to confirm the API is reachable and responding correctly.  It checks:

1. `GET /health` — unauthenticated liveness probe (no API key required)
2. `GET /status/openclaw-agent.service` — authenticated status check

A passing run confirms both that the API process is running and that the
`VPS_CONTROL_API_KEY` secret in GitHub matches the key on the VPS.

---

## Secrets Required

| Secret                | Used for                                                     |
|-----------------------|--------------------------------------------------------------|
| `VPS_CONTROL_API_KEY` | Auth header for direct VPS API calls (Vercel + GitHub)       |
| `VPS_SSH_KEY`         | Private RSA key — SSHes into the VPS as `jacks` (fallback)  |
| `ANSIBLE_INVENTORY`   | Ansible inventory content (injected at runtime, fallback)    |
| GitHub API token      | Link's own token — used to POST `workflow_dispatch` (fallback)|

---

## Code Push / Restart Cycle

When code is pushed to a bot repo (e.g. `openclaw-crypto`):

1. **Push** — Lee pushes new code to the bot's GitHub repo
2. **Deploy** — Link calls `POST /deploy/<service>` on the VPS API (`git pull` + restart)
3. **Verify** — Link calls `GET /status/<service>` to confirm the service is active

> ⚠️ For `openclaw-crypto.service` (real GBP), always check for open Kraken
> positions before triggering a restart or deploy.

---

*Last updated: 2026-05-06 — rewritten to reflect direct VPS API as primary path; GitHub Actions documented as fallback*
