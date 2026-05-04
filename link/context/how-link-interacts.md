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

## How Link Calls the GitHub API

Link uses the GitHub API `workflow_dispatch` endpoint.  The required inputs are:

| Input        | Type   | Required | Values                                          |
|--------------|--------|----------|-------------------------------------------------|
| `action`     | choice | Yes      | `status-all`, `systemd-status`, `systemd-logs`, `systemd-restart` |
| `tail_lines` | string | No       | Number of log lines (default `50`)              |

**Example — restart all systemd bots:**
```
POST https://api.github.com/repos/leeheggan-droid/openclaw-control/actions/workflows/link.yml/dispatches
Authorization: Bearer <GITHUB_TOKEN>
Content-Type: application/json

{
  "ref": "main",
  "inputs": {
    "action": "systemd-restart",
    "tail_lines": "50"
  }
}
```

**Example — fetch last 20 lines of logs:**
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

> The API returns `HTTP 204 No Content` immediately.  The workflow run starts
> asynchronously.  To check results, Link must poll the GitHub API for the run
> status and logs.

---

## How Link Reads Results

After triggering a workflow, Link can check the outcome via:

```
GET /repos/leeheggan-droid/openclaw-control/actions/runs?event=workflow_dispatch
```

This returns the list of workflow runs.  Link takes the most recent one and reads
its logs once the `status` is `completed`.

```
GET /repos/leeheggan-droid/openclaw-control/actions/runs/{run_id}/logs
```

The logs contain the Ansible output (systemctl status lines, journald entries,
etc.) printed by the `debug:` tasks in each playbook.

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

| Secret             | Used for                                           |
|--------------------|----------------------------------------------------|
| `VPS_SSH_KEY`      | Private RSA key — SSHes into the VPS as `jacks`    |
| `ANSIBLE_INVENTORY`| Full Ansible inventory file content (injected at runtime) |
| GitHub API token   | Link's own GitHub token — used to POST workflow_dispatch |

The GitHub API token used by Link must have `actions:write` permission on the
`leeheggan-droid/openclaw-control` repository.

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

*Last updated: 2026-05-04*
