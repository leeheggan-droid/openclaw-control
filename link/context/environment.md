# Link — Environment Context

> **Single source of truth for Link's active execution context.**
> Link must read this file before taking any operational action.
> A human operator must update this file whenever the execution environment changes.

---

## Active Environment

| Field        | Value |
|--------------|-------|
| Context      | `GITHUB_ACTIONS` |
| Last updated | 2026-05-04 22:40 UTC |
| Updated by   | Copilot |
| Notes        | Triggered via GitHub Actions workflow_dispatch. Workflow input: `action`. Secrets: `VPS_SSH_KEY`, `ANSIBLE_INVENTORY`. Token requires `Actions: write` — see `link/context/github-token.md`. |

---

## How to Trigger an Action (GITHUB_ACTIONS)

Go to **GitHub UI → Actions → Link Control → Run workflow** and select an action.

Or via GitHub API (for Link) — the workflow input is named **`action`**:
```
POST /repos/leeheggan-droid/openclaw-control/actions/workflows/link.yml/dispatches
Authorization: Bearer <GITHUB_TOKEN>   ← requires Actions: write (fine-grained) or workflow scope (classic PAT)
Content-Type: application/json

{ "ref": "main", "inputs": { "action": "<action>", "tail_lines": "10" } }
```

> See `link/context/github-token.md` for token setup and verification steps.

### Available Actions & Inputs

| Input        | Type   | Required | Values / Notes                                                                   |
|--------------|--------|----------|----------------------------------------------------------------------------------|
| `action`     | choice | **Yes**  | `status-all`, `systemd-status`, `systemd-logs`, `systemd-restart`, `logs-systemd` |
| `service`    | string | No       | Systemd service name for `logs-systemd` (default: `crypto-bot`)                  |
| `tail_lines` | string | No       | Log lines to fetch — `systemd-logs` and `logs-systemd` only (default: `50`)      |

| Action           | Effect                                                               | Destructive? |
|------------------|----------------------------------------------------------------------|--------------|
| `status-all`     | Full server status — all systemd bots                                | No (default) |
| `systemd-status` | Status of all 4 systemd bots (agent, crypto, alpaca, linkedin-news)  | No |
| `systemd-logs`   | Fetch logs from all 4 systemd bots (`tail_lines` controls count)     | No |
| `systemd-restart`| Restart all 4 systemd bots (agent, crypto, alpaca, linkedin-news)    | No |
| `logs-systemd`   | Fetch logs from one specific service (set `service` + `tail_lines`)  | No |

### Quick reference — most common requests

| What Link is asked                                  | `action`         | `service`                  | `tail_lines` |
|-----------------------------------------------------|------------------|----------------------------|--------------|
| "Last 10 lines of the crypto bot logs"              | `systemd-logs`   | —                          | `10`         |
| "Last 30 lines of the agent only"                   | `logs-systemd`   | `openclaw-agent.service`   | `30`         |
| "Show me the status of all bots"                    | `status-all`     | —                          | —            |
| "Is the crypto bot running?"                        | `systemd-status` | —                          | —            |
| "Restart the agent after a code push"               | `systemd-restart`| —                          | —            |

---

## How Link Triggers Actions — Full Flow

See `link/context/how-link-interacts.md` for the complete end-to-end diagram.

Short version:
1. Link calls the GitHub API (`workflow_dispatch`)
2. `link.yml` GitHub Actions workflow runs on `ubuntu-latest`
3. Ansible SSHes into the VPS using `VPS_SSH_KEY`
4. Ansible runs the appropriate `systemctl` or `journalctl` command
5. Output appears in the GitHub Actions run log

---

## Context Definitions

### `LOCAL_SSH`

- Link or the operator is running on a **local machine** with direct SSH access to the VPS.
- Ansible is installed and available on `PATH` (`ansible --version` confirms).
- SSH key `~/.ssh/id_rsa` is present locally and its public half is authorised on the VPS for user `jacks`.
- Inventory is `ansible/inventory` (committed to the repo; no secrets).
- **Trigger method:**

  ```bash
  ansible-playbook -i ansible/inventory ansible/site.yml --tags <action>
  ```

### `LOCAL_DOCKER`

- The operator is running on a **local machine** (or the VPS itself) with **Docker** installed.
- No local Ansible installation is required — Ansible runs inside the container.
- SSH key `~/.ssh/id_rsa` is present and its public half is authorised on the VPS for user `jacks`.
- The `openclaw-control:ci` image must be built first: `docker build -t openclaw-control:ci .`
- Inventory is `ansible/inventory` (committed; no secrets).
- **Trigger method:**

  ```bash
  docker run --rm \
    -e ANSIBLE_HOST_KEY_CHECKING=False \
    -e ANSIBLE_SSH_ARGS="-F /dev/null" \
    -v "$PWD:/work" \
    -v "$HOME/.ssh:/root/.ssh:ro" \
    openclaw-control:ci \
    -i ansible/inventory ansible/site.yml --tags <action>
  ```

- See `DOCKER_CONTROL.md` for full build and run instructions.

### `GITHUB_ACTIONS`

- Link or the operator triggers the **"Link Control"** `workflow_dispatch` workflow via the GitHub UI or GitHub API.
- Ansible runs on a `ubuntu-latest` GitHub Actions runner.
- SSH key is injected at runtime from the `VPS_SSH_KEY` repository secret.
- Inventory is injected at runtime from the `ANSIBLE_INVENTORY` repository secret.
- **Trigger method:** GitHub UI → Actions → Link Control → Run workflow → select action.

---

## Updating This File

When the execution environment changes (e.g. switching from local to CI, rotating SSH keys, changing VPS IP), update the **Active Environment** table above and record the change date and your name.
