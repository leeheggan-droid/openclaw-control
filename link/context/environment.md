# Link — Environment Context

> **Single source of truth for Link's active execution context.**
> Link must read this file before taking any operational action.
> A human operator must update this file whenever the execution environment changes.

---

## Active Environment

| Field        | Value |
|--------------|-------|
| Context      | `LOCAL_SSH` |
| Last updated | 2026-05-04 00:00 UTC |
| Updated by   | Lee |
| Notes        | Running from local machine. SSH key at `~/.ssh/id_ed25519`. |

---

## How to Trigger a Deployment (LOCAL_SSH)

Run from the **repository root** on your local machine:

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=<action>"
```

Replace `<action>` with one of:

| Action    | Effect                                           | Destructive? |
|-----------|--------------------------------------------------|--------------|
| `status`  | `docker compose ps` — show running containers   | No (default) |
| `up`      | `docker compose up -d` — start all services     | No |
| `down`    | `docker compose down` — stop and remove         | **Yes** |
| `restart` | `docker compose restart` — restart in place     | No |
| `deploy`  | `docker compose pull` + `up -d --remove-orphans`| **Yes** |
| `logs`    | `docker compose logs --tail=100` — last 100 lines | No |

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

> ⚠️ **Known issues with the `GITHUB_ACTIONS` path** (see README TODOs):
>
> 1. `.github/workflows/link.yml` passes `-e "task=…"` but `ansible/site.yml` expects `-e "action=…"`. The playbook will silently default to `status` on every run until this is fixed.
> 2. The workflow writes the inventory to `ansible/inventory.ini` but the `ansible-playbook` command does not pass `-i ansible/inventory.ini`, so it falls back to the committed `ansible/inventory` file instead of the secret. Both bugs must be resolved before the GitHub Actions path is production-ready.

---

## Updating This File

When the execution environment changes (e.g. switching from local to CI, rotating SSH keys, changing VPS IP), update the **Active Environment** table above and record the change date and your name.
