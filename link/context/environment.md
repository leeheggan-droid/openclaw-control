# Link — Environment Context

> **Single source of truth for Link's active execution context.**
> Link must read this file before taking any operational action.
> A human operator must update this file whenever the execution environment changes.

---

## Active Environment

| Field        | Value |
|--------------|-------|
| Context      | `VPS_CONTROL_API` |
| Last updated | 2026-05-06 06:20 UTC |
| Updated by   | Copilot |
| Notes        | Direct VPS HTTP control API is now live at `http://72.61.123.4:8765`. Auth header: `Authorization: Bearer <VPS_CONTROL_API_KEY>`. Vercel env vars: `VPS_CONTROL_API_URL`, `VPS_CONTROL_API_KEY`. GitHub Actions remains available as fallback. |

---

## How to Trigger an Action (VPS_CONTROL_API)

Link now talks directly to the VPS control API instead of waiting for a GitHub Actions runner.

Base URL:
```
http://72.61.123.4:8765
```

Auth for every request except `/health`:
```
Authorization: Bearer <VPS_CONTROL_API_KEY>
```

> The key is stored in Vercel as `VPS_CONTROL_API_KEY`.
> See `link/context/vps-control-api.md` for the full endpoint reference.

### Direct endpoints

| Action needed by Link | Method | Path pattern |
|-----------------------|--------|--------------|
| Health check | `GET` | `/health` |
| Service status | `GET` | `/status/{service}` |
| Recent logs | `GET` | `/logs/{service}?n=<lines>` |
| Restart service | `POST` | `/restart/{service}` |
| Deploy service | `POST` | `/deploy/{service}` |

### Service names

| Bot                  | Exact `service` value              | Notes                                       |
|----------------------|------------------------------------|---------------------------------------------|
| GitHub Agent         | `openclaw-agent.service`           | Safe to restart                             |
| Crypto bot           | `openclaw-crypto.service`          | ⚠️ REAL GBP — check Kraken positions first  |
| Vibe gateway         | `openclaw-vibe-gateway.service`    | Use exact service name                      |
| Alpaca bot           | `alpaca_orb_bite_bot.service`      | Safe — paper trading only                   |
| LinkedIn news (run)  | `linkedin-news.service`            | Use this to run the bot immediately         |
| LinkedIn timer       | `linkedin-news.timer`              | Use this to stop/start the weekly schedule  |

### Quick reference — most common requests

| What Link is asked                                  | Method | Path |
|-----------------------------------------------------|--------|------|
| "Is the agent running?"                            | `GET`  | `/status/openclaw-agent.service` |
| "Last 10 lines of the agent log"                   | `GET`  | `/logs/openclaw-agent.service?n=10` |
| "Restart the agent"                                | `POST` | `/restart/openclaw-agent.service` |
| "Deploy the agent"                                 | `POST` | `/deploy/openclaw-agent.service` |
| "Is the crypto bot running?"                       | `GET`  | `/status/openclaw-crypto.service` |
| "Last 30 lines of the crypto bot logs"             | `GET`  | `/logs/openclaw-crypto.service?n=30` |
| "Restart the LinkedIn timer"                       | `POST` | `/restart/linkedin-news.timer` |

---

## How Link Triggers Actions — Full Flow

See `link/context/how-link-interacts.md` for the complete end-to-end description.

Short version:
1. Link reads `VPS_CONTROL_API_URL` and `VPS_CONTROL_API_KEY` from Vercel env vars
2. Link sends an authenticated HTTP request to the VPS control API
3. The API validates the service name against the allow-list
4. The API runs `systemctl`, `journalctl`, or deploy logic on the VPS
5. JSON response returns immediately to Link

---

## Context Definitions

### `VPS_CONTROL_API`

- Link runs on Vercel and talks directly to the VPS over HTTP.
- Control API listens on `72.61.123.4:8765`.
- Auth uses `Authorization: Bearer <VPS_CONTROL_API_KEY>`.
- Fast path for status, logs, restart, and deploy operations.
- GitHub Actions is no longer the primary control path for these operations.

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
- Use this as a fallback path if the direct control API is unavailable.
- **Trigger method:** GitHub UI → Actions → Link Control → Run workflow → select action.

---

## Updating This File

When the execution environment changes (e.g. switching from local to CI, rotating SSH keys, changing VPS IP, or moving between GitHub Actions and the direct control API), update the **Active Environment** table above and record the change date and your name.
