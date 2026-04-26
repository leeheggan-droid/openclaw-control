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
   │    └─ returns JSON: {needs_action, urgency, summary, recommended_action}
   │
   ├─ needs_action=false → silent; update last_clear timestamp
   │
   └─ needs_action=true  → push to findings queue; increment unread badge
```

### Components

| Layer    | File                                              | Role                                                             |
|----------|---------------------------------------------------|------------------------------------------------------------------|
| Agent    | `openclaw_control/agents/investigate_agent.py`    | Reviews system data; returns structured JSON verdict            |
| Service  | `openclaw_control/service.py`                     | `start/stop_autopilot`, `get_autopilot_status/findings/ack`     |
| Config   | `openclaw_control/config.py`                      | `autopilot_interval` (env: `OPENCLAW_AUTOPILOT_INTERVAL`)       |
| API      | `web_app.py`                                      | `/autopilot/start|stop|status|findings|ack`                      |
| UI       | `web_app.py` (inline HTML/JS)                     | Autopilot tab with start/stop, live countdown, findings feed    |

---

## Operator Interaction Model

| Condition              | System behaviour                                              | Operator interrupted? |
|------------------------|---------------------------------------------------------------|-----------------------|
| Everything healthy     | Silent — updates `last_clear` timestamp, no finding recorded | ❌ No                 |
| Anomaly detected       | Finding pushed to queue; tab badge incremented               | ✅ Yes (badge only)   |
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
 │  └──────────────────────────────────────────────────────────────────────┘  │
 │                                                                             │
 │  ┌─ 2025-04-26 04:46:00 UTC  MEDIUM ─────────────────────────────────────┐ │
 │  │  Repeated KeyError exceptions in the last 50 log lines.               │ │
 │  │  → Check the orchestrator config for missing keys.                    │ │
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
- [ ] If SSH host is configured and an anomaly is found, badge appears on the Autopilot tab
- [ ] Click **⏸ Stop** — dot returns to grey, status shows "Stopped · last check Xs ago"
- [ ] Click **✓ Mark all read** — badge disappears, finding rows lose the red left border
