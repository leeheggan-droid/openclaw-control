# Service: openclaw-agent

> Persistent memory for the `openclaw-agent.service` systemd unit.
> For the full host context (all bots), see `link/context/services/host-overview.md`.

---

## Service Identity

| Field              | Value                                                   |
|--------------------|---------------------------------------------------------|
| systemd unit       | `openclaw-agent.service`                                |
| Description        | GitHub Issue Polling Agent — automated task-intake pipeline |
| Execution model    | **systemd-managed, native host — NOT Docker**           |
| Entrypoint         | `/opt/openclaw-agent/venv/bin/python /opt/openclaw-agent/agent.py` |
| Working directory  | `/opt/openclaw-agent/`                                  |
| Required env vars  | `GITHUB_REPO` (and any other vars defined in unit file) |
| VPS                | `srv1501082` / `72.61.123.4`                            |
| VPS user           | `jacks`                                                 |
| Logs               | journald only                                           |

---

## What Does the Agent Do?

The openclaw-agent is the **task-intake pipeline** for this entire stack.  It
runs as a persistent background process and polls a target GitHub repository
(set via `GITHUB_REPO`) for new issues.  When a new issue is detected, the
agent reads the issue body and uses it as a task prompt — typically triggering
a **GitHub Copilot coding-agent session** to carry out the requested code
change automatically.

This is the mechanism by which a GitHub issue (such as the one that created this
PR) drives an automated code change with no human keyboard input beyond opening
the issue.

**Prompt flow:**
1. Issue opened in the target GitHub repo
2. `openclaw-agent.service` polls and detects the new issue
3. Agent submits the issue body as a Copilot task prompt
4. Copilot agent clones the repo, makes changes, and opens a PR

The "prompt" is therefore the issue body itself — write it clearly and
specifically so the Copilot agent understands the full intent.

---

## Authoritative Commands

### Check status (safe — run freely)

```bash
systemctl status openclaw-agent.service
```

### Stream live logs (safe — run freely)

```bash
journalctl -u openclaw-agent.service -f
```

### View recent logs

```bash
journalctl -u openclaw-agent.service --no-pager -n 50
```

### Inspect the service unit file (safe — run freely)

```bash
systemctl cat openclaw-agent.service
```

### Restart (confirm intent before running)

```bash
systemctl restart openclaw-agent.service
```

### Stop ⚠️ destructive — require explicit user confirmation

```bash
systemctl stop openclaw-agent.service
```

### Start

```bash
systemctl start openclaw-agent.service
```

---

## Critical Notes

> ⚠️ **This agent does NOT run in Docker.** Never use `docker ps` or `docker logs`
> to check its state — they will show nothing and give a false "not running" result.

- The service requires `GITHUB_REPO` to be set (and any other environment variables
  declared in its unit file). If the variable is missing, the service will fail on
  startup — check `journalctl -u openclaw-agent.service` for the exact error.
- All log output goes to journald (`journalctl`). There are no separate log files.
- After a `git pull` in `/opt/openclaw-agent/`, restart the service to pick up changes:
  `sudo systemctl restart openclaw-agent.service`
- The **issue body is the prompt** — when writing a GitHub issue intended for the
  agent to process, be explicit: include the goal, relevant file paths, constraints,
  and acceptance criteria.

---

*Last updated: 2026-05-05*
