# Copilot Instructions

## System Overview

This repository (`openclaw-control`) is an **Ansible-based control layer** for managing
a stack of Python bot services running as native systemd units on an Ubuntu VPS
(`srv1501082` / `72.61.123.4`, SSH user `jacks`).

Operations are triggered via one of three paths — always read
`link/context/environment.md` to confirm which is active before taking any action:

| Context | How to trigger |
|---|---|
| `LOCAL_SSH` | `ansible-playbook -i ansible/inventory ansible/site.yml --tags <tag>` |
| `LOCAL_DOCKER` | `docker run openclaw-control:ci -i ansible/inventory ansible/site.yml --tags <tag>` |
| `GITHUB_ACTIONS` *(active)* | `workflow_dispatch` on `.github/workflows/link.yml` via GitHub UI or API |

The ansible/inventory file is committed (for LOCAL_SSH). For GITHUB_ACTIONS, the
inventory is injected at runtime from the `ANSIBLE_INVENTORY` secret into
`ansible/inventory.ini`. Both are intentional — they are different files.

---

## The openclaw-agent

`openclaw-agent.service` is the **GitHub Issue Polling Agent** — the central piece of
the automated task pipeline. It runs as a native systemd service on the VPS; it is
**not Docker-based**.

| Field | Value |
|---|---|
| systemd unit | `openclaw-agent.service` |
| Script | `/opt/openclaw-agent/venv/bin/python /opt/openclaw-agent/agent.py` |
| Working directory | `/opt/openclaw-agent/` |
| Required env var | `GITHUB_REPO` (plus any others declared in the unit file) |
| Logs | `journalctl -u openclaw-agent.service` |

**What it does:** The agent polls a target GitHub repository for new issues. When it
detects a new issue it uses the issue body as a task prompt and dispatches it — for
example by triggering a GitHub Copilot coding-agent session. This is the mechanism
by which GitHub issues drive automated Copilot tasks in this repo.

**Critical — never use Docker to inspect it.** `docker ps` and `docker logs` will
show nothing and give a false "not running" reading. Always use:
- `systemctl status openclaw-agent.service` — check health
- `journalctl -u openclaw-agent.service -n 50` — read logs
- `systemctl cat openclaw-agent.service` — inspect environment variables

After any code change in `/opt/openclaw-agent/`, restart the service to pick up
changes: `sudo systemctl restart openclaw-agent.service`. Confirm `GITHUB_REPO`
is set in the unit file before restarting — a missing variable causes an immediate
startup failure.

---

## VPS Services

All services run as **native systemd units** on `srv1501082`. There is no Docker
web app on the VPS. `www.leeheggan.tech` runs on Vercel (the separate Link repo).

| Service | Purpose | Restart risk |
|---|---|---|
| `openclaw-agent.service` | GitHub Issue Polling Agent | Low — no live money |
| `openclaw-crypto.service` | Kraken crypto trading bot | **⚠️ REAL GBP — check open positions first** |
| `openclaw-vibe-gateway.service` | Vibe gateway (wraps Docker) | Low |
| `alpaca_orb_bite_bot.service` | Alpaca paper trading bot | Low — paper only |
| `linkedin-news.timer` | LinkedIn DC news bot (Sun 22:00 UTC) | Low |

---

## Link AI Assistant

**Link** (`www.leeheggan.tech`) is a Vercel-deployed AI assistant that manages the
VPS bot stack via the GitHub API. It calls `workflow_dispatch` on `link.yml` to run
Ansible playbooks — it never SSHes directly into the VPS.

Context files that Link reads at the start of each session live in `link/context/`.
When modifying any of these files, keep the format consistent (markdown tables,
clear headings) so that Link can parse them reliably.

---

## Ansible Structure

| Path | Purpose |
|---|---|
| `ansible/site.yml` | Root playbook; routes to task files by Ansible tag |
| `ansible/tasks/status-all.yml` | One-line `systemctl status` for all 4 bots |
| `ansible/tasks/systemd-status.yml` | Detailed `systemctl status` for all 4 bots |
| `ansible/tasks/systemd-restart.yml` | `systemctl restart` for all 4 bots |
| `ansible/tasks/systemd-logs.yml` | `journalctl` logs for all 4 bots |
| `ansible/tasks/logs-systemd.yml` | `journalctl` logs for one specific service |
| `ansible/inventory` | Committed host config (LOCAL_SSH) |
| `ansible/inventory.ini` | Runtime-injected (GITHUB_ACTIONS) — never commit |

Available action tags: `status-all`, `systemd-status`, `systemd-restart`,
`systemd-logs`, `logs-systemd`.

---

## General Rules
- Keep changes minimal: prefer editing one file per task unless the issue explicitly allows more.
- Never introduce secrets, credentials, or environment variables into source code.
- Never run destructive operations (e.g. `DROP`, `DELETE *`, `rm -rf`) without an explicit instruction.
- Do not install new dependencies unless the issue explicitly requests it.
- Match the existing code style and conventions in the file being edited.
- Before restarting `openclaw-crypto.service`, always confirm there are no open Kraken positions.

## Pull Requests
- Write a short, descriptive PR title and a one-paragraph summary of what changed and why.
- List every file changed and the reason in the PR description.
- Reference the originating issue number (e.g. `Closes #123`).

## Safety Checklist (add to every PR description)
- [ ] No secrets or credentials added to source code
- [ ] No destructive operations introduced
- [ ] Changes limited to the minimum required by the issue
- [ ] Ansible playbook validated (no web app server in this repo)
