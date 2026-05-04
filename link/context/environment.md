# Link — Environment Context

> **Single source of truth for Link's active execution context.**
> Link must read this file before taking any operational action.
> A human operator must update this file whenever the execution environment changes.

---

## Active Environment

| Field        | Value |
|--------------|-------|
| Context      | `GITHUB_ACTIONS` |
| Last updated | 2026-05-04 12:45 UTC |
| Updated by   | Lee |
| Notes        | Triggered via GitHub Actions workflow_dispatch. Secrets: `VPS_SSH_KEY`, `ANSIBLE_INVENTORY`. |

---

## How to Trigger an Action (GITHUB_ACTIONS)

Go to **GitHub UI → Actions → Link Control → Run workflow** and select an action.

Or via GitHub API (for Link):
```
POST /repos/leeheggan-droid/openclaw-control/actions/workflows/link.yml/dispatches
{ "ref": "main", "inputs": { "action": "<action>", "tail_lines": "10" } }
```

### Available Actions

| Action           | Effect                                                          | Destructive? |
|------------------|-----------------------------------------------------------------|--------------|
| `status-all`     | Full server status — Docker containers + all systemd bots       | No (default) |
| `status`         | Show Openclaw Docker container status                           | No |
| `systemd-status` | Status of all 3 systemd bots (crypto, alpaca, linkedin-news)   | No |
| `logs`           | Fetch logs from Openclaw Docker containers                      | No |
| `systemd-logs`   | Fetch logs from all 3 systemd bots (`tail_lines` controls count)| No |
| `restart`        | Restart Openclaw Docker stack                                   | No |
| `systemd-restart`| Restart all 3 systemd bots                                      | No |
| `up`             | `docker compose up -d` — start Docker stack                    | No |
| `down`           | `docker compose down` — stop and remove Docker stack            | **Yes** |
| `deploy`         | `docker compose pull` + `up -d --remove-orphans`               | **Yes** |

### The `tail_lines` Input

Only applies to `logs` and `systemd-logs` actions. Defaults to `50`.
Set to `10` for a quick last-10-lines check.

### Quick reference — most common requests

| What Link is asked                                  | Action to use    | tail_lines |
|-----------------------------------------------------|------------------|------------|
| "Last 10 lines of the crypto bot logs"              | `systemd-logs`   | `10`       |
| "Show me the status of all bots"                    | `status-all`     | —          |
| "Is the crypto bot running?"                        | `systemd-status` | —          |
| "Show me the openclaw app logs"                     | `logs`           | `50`       |

---

## Context Definitions

### `LOCAL_SSH`

- Link or the operator is running on a **local machine** with direct SSH access to the VPS.
- Ansible is installed and available on `PATH` (`ansible --version` confirms).
- SSH key `~/.ssh/id_ed25519` is present locally and its public half is authorised on the VPS for user `jacks`.
- Inventory is `ansible/inventory` (committed to the repo; no secrets).
- **Trigger method:**

  ```bash
  ansible-playbook -i ansible/inventory ansible/site.yml -e "action=<action>"
  ```

### `GITHUB_ACTIONS`

- Link or the operator triggers the **"Link Control"** `workflow_dispatch` workflow via the GitHub UI or GitHub API.
- Ansible runs on a `ubuntu-latest` GitHub Actions runner.
- SSH key is injected at runtime from the `VPS_SSH_KEY` repository secret.
- Inventory is injected at runtime from the `ANSIBLE_INVENTORY` repository secret.
- **Trigger method:** GitHub UI → Actions → Link Control → Run workflow → select action.

---

## Updating This File

When the execution environment changes (e.g. switching from local to CI, rotating SSH keys, changing VPS IP), update the **Active Environment** table above and record the change date and your name.
