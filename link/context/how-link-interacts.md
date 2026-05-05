# How Link Interacts With openclaw-control

> This file documents the complete end-to-end flow by which Link (the Vercel AI
> assistant at www.leeheggan.tech) triggers operational actions on the VPS via
> this repository.

---

## Overview

Link does **not** SSH directly into the VPS.  Instead, it uses the **GitHub API**
to trigger a `workflow_dispatch` event on `link.yml`.  A GitHub Actions runner
then SSHes into the VPS and executes the Ansible playbook.  This keeps SSH
credentials off Link's runtime and produces a full audit trail in GitHub Actions
logs.

---

## End-to-End Flow

```
┌──────────────────────────────────────────────┐
│  Link (Vercel / www.leeheggan.tech)          │
│                                              │
│  User says: "restart the agent"              │
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
│  Ansible runs:                               │
│  systemctl restart openclaw-agent.service    │
│  systemctl restart openclaw-crypto.service   │
│  systemctl restart alpaca_orb_bite_bot       │
│  systemctl restart linkedin-news.timer       │
└──────────────────────────────────────────────┘
                    │
                    ▼
        Results → GitHub Actions run log
        (Link reads log via GitHub API if needed)
```

---

## GitHub Token Requirements

Link needs a GitHub API token with permission to trigger workflow runs on this
repository.  **Without the correct token, every dispatch will return 401 or 403
and the workflow will never start.**

| Token type            | Required permission / scope                              |
|-----------------------|----------------------------------------------------------|
| Fine-grained PAT      | **Actions: Read and Write** on `leeheggan-droid/openclaw-control` |
| Classic PAT           | **`workflow`** scope                                     |

> See `link/context/github-token.md` for a step-by-step guide on verifying the
> token works before making operational calls.

---

## How Link Calls the GitHub API

Link uses the GitHub API `workflow_dispatch` endpoint.  The workflow input is
named **`action`** (not `task`).

| Input        | Type   | Required | Values                                                                                                       |
|--------------|--------|----------|--------------------------------------------------------------------------------------------------------------|
| `action`     | choice | **Yes**  | `status-all`, `systemd-status`, `systemd-logs`, `systemd-restart`, `systemd-stop`, `systemd-start`, `logs-systemd` |
| `service`    | string | No       | Exact unit name — **required** for `systemd-stop`, `systemd-start`, `logs-systemd` (default: `crypto-bot`)  |
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

**Example — fetch last 20 lines of logs from all bots:**
```
POST …/dispatches
{
  "ref": "main",
  "inputs": {
    "action": "systemd-logs",
    "tail_lines": "20"
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
> GitHub API to know whether it succeeded (see below).

---

## How Link Reads Results

After triggering a workflow, poll the GitHub API using these exact steps:

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
Returns a ZIP archive containing the Ansible output (systemctl status lines,
journald entries, etc.) printed by the `debug:` tasks in each playbook.

> Note: Log download requires the same token with **Actions: Read** permission.

---

## Code Push / Restart Cycle

When code is pushed to a bot repo (e.g. `openclaw-crypto`), the workflow to pick
up the changes is:

1. **Push** — Lee pushes new code to the bot's GitHub repo
2. **Pull on VPS** — Lee (or Link) SSHes in and runs `git pull` in the bot's
   working directory, **or** the systemd service unit itself calls `git pull`
   before starting (if configured that way)
3. **Restart** — Link triggers `systemd-restart` via the GitHub API
4. **Verify** — Link triggers `systemd-status` or `systemd-logs` to confirm

> ⚠️ For `openclaw-crypto.service` (real GBP), always check for open Kraken
> positions before triggering a restart.

---

## Secrets Required

| Secret             | Used for                                                        |
|--------------------|-----------------------------------------------------------------|
| `VPS_SSH_KEY`      | Private RSA key — SSHes into the VPS as `jacks`                 |
| `ANSIBLE_INVENTORY`| Full Ansible inventory file content (injected at runtime)       |
| GitHub API token   | Link's own token — used to POST `workflow_dispatch`             |

The GitHub API token used by Link must have:
- **Fine-grained PAT:** `Actions: Read and Write` on `leeheggan-droid/openclaw-control`
- **Classic PAT:** `workflow` scope

> See `link/context/github-token.md` for verification steps and troubleshooting.

---

## Limitations & Notes

- **No real-time streaming** — Link cannot stream logs in real-time; it must wait
  for the workflow run to complete, then fetch the log archive.
- **Latency** — from trigger to completion is typically 30–90 seconds (runner
  spin-up + Ansible execution).
- **No rollback** — if a restart leaves a service broken, Link must trigger
  another `systemd-restart` after the underlying issue is fixed.
- **Single VPS** — this entire system manages one VPS (`srv1501082`).  Adding a
  second host requires updating `ansible/inventory` and re-issuing the
  `ANSIBLE_INVENTORY` secret.

---

*Last updated: 2026-05-04 — input renamed `task`→`action`, added `service`/`logs-systemd`, token permissions, polling steps*
