# Autopilot Investigate

## Overview

The Autopilot Investigate feature adds **autonomous, scheduled system investigation** to the OpenClaw multi-agent cockpit. The system runs SSH health checks on a configurable interval, feeds the results to the Investigate Agent, and **only surfaces a finding to the operator when action is required**. Routine all-clear cycles are discarded silently.

---

## Architecture

```
Background loop (daemon thread)
   │
   ├─ every OPENCLAW_AUTOPILOT_INTERVAL seconds
   │
   ├─ SSH: docker ps + docker logs --tail=100 openclaw-orchestrator
   │
   ├─ Investigate Agent (LLM, no tools)
   │    └─ returns JSON: {needs_action, urgency, summary, recommended_action, action_type}
   │
   ├─ needs_action=false  → silent; update last_clear timestamp
   │
   └─ needs_action=true
        ├─ action_type="github_issue"  → open GitHub issue automatically (code/config fix)
        ├─ action_type="vibe_action"   → queue Vibe command (approval gated, state change)
        └─ action_type="none"/unknown  → surface as plain finding for manual review
```

### Components

| Layer    | File                                              | Role                                                             |
|----------|---------------------------------------------------|------------------------------------------------------------------|
| Agent    | `openclaw_control/agents/investigate_agent.py`    | Reviews system data; returns structured JSON verdict with `action_type` |
| Service  | `openclaw_control/service.py`                     | `start/stop_autopilot`, `get_autopilot_status/findings/ack`, `_autopilot_open_github_issue` |
| Config   | `openclaw_control/config.py`                      | `autopilot_interval` (env: `OPENCLAW_AUTOPILOT_INTERVAL`), `github_token`, `github_repo` |
| API      | `web_app.py`                                      | `/autopilot/start|stop|status|findings|ack`                      |
| UI       | `web_app.py` (inline HTML/JS)                     | Autopilot tab with start/stop, live countdown, findings feed    |

---

## Escalation Logic

After the Investigate Agent classifies an anomaly it sets `action_type` to one of three values:

| `action_type`    | Trigger condition                                               | Autopilot action                                                 | Operator interrupted? |
|------------------|-----------------------------------------------------------------|------------------------------------------------------------------|-----------------------|
| `"none"`         | `needs_action=false`                                           | Silent — updates `last_clear`, no finding recorded              | ❌ No                 |
| `"github_issue"` | Fix requires code or config file change                        | Opens a GitHub issue automatically (uses `GITHUB_TOKEN`)        | ✅ Badge + issue link |
| `"vibe_action"`  | Fix requires a runtime state change (restart, run command)     | Generates a Vibe command and queues it for operator approval     | ✅ Badge + approve btn|

### `github_issue` path

When the agent classifies the anomaly as requiring a code/config change, the service:
1. Calls the GitHub Issues API to create an issue in the configured repo (`GITHUB_REPO`).
2. Labels the issue `autopilot` and `bug`.
3. Populates the body with: anomaly summary, recommended action, SSH output, and acceptance criteria.
4. Records the issue URL and number on the finding so the UI can render a direct link.

> **Requirement:** `GITHUB_TOKEN` must be set in `.env` with `repo` scope. If the token is absent or the API call fails, the escalation falls back to surfacing a plain finding for manual review.

### `vibe_action` path

When the agent classifies the anomaly as a runtime state issue, the service:
1. Calls the Vibe Planner agent to generate a concrete shell command.
2. Records the command on the finding.
3. The UI renders an **✅ Approve & Execute via Vibe** button — **no command runs until the operator clicks it**.

---

## Operator Interaction Model

| Condition              | System behaviour                                              | Operator interrupted? |
|------------------------|---------------------------------------------------------------|-----------------------|
| Everything healthy     | Silent — updates `last_clear` timestamp, no finding recorded | ❌ No                 |
| Anomaly: code/config   | GitHub issue opened automatically; finding shows issue link  | ✅ Yes (badge + link) |
| Anomaly: state change  | Vibe command queued; approval button shown in finding        | ✅ Yes (badge + btn)  |
| Anomaly: other         | Finding surfaced for manual review                           | ✅ Yes (badge only)   |
| Budget exhausted       | AI analysis skipped; SSH commands still run                  | ❌ No                 |
| SSH host not configured| Investigation skipped; last_run still updated                | ❌ No                 |

Urgency levels returned by the Investigate Agent:

| Level    | Meaning                                      | Example                         |
|----------|----------------------------------------------|---------------------------------|
| `high`   | Immediate action required                    | Container down, system HALTED   |
| `medium` | Should review soon                           | Repeated errors, degraded state |
| `low`    | Informational; worth knowing but not urgent  | Unusual log pattern             |

---

## Environment Variables

| Variable                       | Default | Purpose                                             |
|--------------------------------|---------|-----------------------------------------------------|
| `OPENCLAW_AUTOPILOT_INTERVAL`  | `300`   | Seconds between investigation cycles (min: 30 s)  |
| `GITHUB_TOKEN`                 | `""`    | GitHub PAT with `repo` scope — required for auto issue creation |
| `GITHUB_REPO`                  | `leeheggan-droid/openclaw-control` | Target repo for auto-created issues |

---

## API Reference

### `POST /autopilot/start`

Start the background investigation loop.

**Query parameter (optional):** `interval` (int, seconds) — overrides the server default for this run.

**Response:**
```json
{"status": "started", "interval": 300}
```
or `{"status": "already_running"}` if already running.

---

### `POST /autopilot/stop`

Stop the background investigation loop.

**Response:**
```json
{"status": "stopping"}
```

---

### `GET /autopilot/status`

Return the current autopilot state.

**Response:**
```json
{
  "running": true,
  "interval": 300,
  "last_run": "2025-04-26T05:00:00Z",
  "last_clear": "2025-04-26T04:55:00Z",
  "unread": 2,
  "finding_count": 5,
  "seconds_until_next_run": 248
}
```

---

### `GET /autopilot/findings`

Return findings from the given cursor position.

**Query parameter:** `cursor` (int, default 0)

**Response:**
```json
{
  "findings": [
    {
      "id": 1,
      "t": "2025-04-26T05:01:00Z",
      "urgency": "high",
      "summary": "Container openclaw-orchestrator is not running.",
      "recommended_action": "Run docker compose up -d to restart.",
      "action_type": "vibe_action",
      "vibe_command": "cd /opt/openclaw-crypto && docker compose up -d",
      "github_issue_url": "",
      "github_issue_number": null,
      "acked": false
    },
    {
      "id": 2,
      "t": "2025-04-26T05:10:00Z",
      "urgency": "medium",
      "summary": "KeyError in orchestrator config suggests a missing key.",
      "recommended_action": "Add the missing key to the config file.",
      "action_type": "github_issue",
      "vibe_command": "",
      "github_issue_url": "https://github.com/leeheggan-droid/openclaw-crypto/issues/42",
      "github_issue_number": 42,
      "acked": false
    }
  ]
}
```

---

### `POST /autopilot/ack`

Acknowledge all findings (resets the unread badge to 0).

**Response:**
```json
{"status": "ok"}
```

---

## UI Flow

```
 ┌─ Autopilot tab active ─────────────────────────────────────────────────────┐
 │                                                                             │
 │  ● Running  ·  last check 2m ago  ·  next in 3m     [⏸ Stop] [✓ Mark all read] │
 │                                                                             │
 │  ┌─ 2025-04-26 05:01:00 UTC  HIGH ──────────────────────────────────────┐  │
 │  │  Container openclaw-orchestrator is not running.                     │  │
 │  │  → Run docker compose up -d to restart.                              │  │
 │  │  ⚙ Command: cd /opt/openclaw-crypto && docker compose up -d         │  │
 │  │  [✅ Approve & Execute via Vibe]                                      │  │
 │  └──────────────────────────────────────────────────────────────────────┘  │
 │                                                                             │
 │  ┌─ 2025-04-26 05:10:00 UTC  MEDIUM ─────────────────────────────────────┐ │
 │  │  KeyError in orchestrator config suggests a missing key.              │ │
 │  │  → Add the missing key to the config file.                            │ │
 │  │  🐛 GitHub issue #42 created automatically  [↗ link]                  │ │
 │  └────────────────────────────────────────────────────────────────────────┘ │
 └─────────────────────────────────────────────────────────────────────────────┘
```

When a new finding arrives, the **Autopilot** tab button shows a red badge with the unread count. Clicking **✓ Mark all read** (or visiting the tab) clears the badge.

---

## Local Test Checklist

- [ ] `git pull`
- [ ] `uvicorn web_app:app --reload`
- [ ] Open browser → navigate to the cockpit
- [ ] Click the **Autopilot** tab — status bar shows "Stopped", empty-state message visible
- [ ] Click **▶ Start** — dot turns green, status text changes to "Running · next in 5m"
- [ ] Wait for first investigation cycle (or reduce `OPENCLAW_AUTOPILOT_INTERVAL=30` in `.env`)
- [ ] If SSH host is configured and an anomaly requiring a state change is found → badge appears + Vibe approve button visible
- [ ] If an anomaly requiring code/config change is found → badge appears + GitHub issue link visible in finding
- [ ] Click **⏸ Stop** — dot returns to grey, status shows "Stopped · last check Xs ago"
- [ ] Click **✓ Mark all read** — badge disappears, finding rows lose the red left border
