# VPS Control API

> Direct HTTP API running on the VPS at port **8765**. Provides sub-second
> status checks, log fetches, restarts, and deploys — without waiting 30–90 s
> for a GitHub Actions runner.

---

## Base URL

```
http://72.61.123.4:8765
```

## Auth

Every request (except `/health`) requires:

```
Authorization: Bearer <VPS_CONTROL_API_KEY>
```

The key is stored in Vercel as `VPS_CONTROL_API_KEY`.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Unauthenticated liveness probe |
| `GET` | `/status/{service}` | `systemctl is-active {service}` |
| `GET` | `/logs/{service}?n=50` | Last N lines of journald logs |
| `POST` | `/restart/{service}` | `systemctl restart {service}` |
| `POST` | `/deploy/{service}` | `git pull` then `systemctl restart` |

### Allowed service names

```
openclaw-agent.service
openclaw-crypto.service
openclaw-vibe-gateway.service
alpaca_orb_bite_bot.service
linkedin-news.timer
linkedin-news.service
```

---

## Response shapes

### GET /status/{service}
```json
{ "service": "openclaw-agent.service", "active": true, "state": "active" }
```

### GET /logs/{service}?n=20
```json
{
  "service": "openclaw-agent.service",
  "lines": ["2026-05-06T05:00:01+0000 srv1 openclaw-agent[1234]: polling…"],
  "returncode": 0
}
```

### POST /restart/{service}
```json
{ "service": "openclaw-agent.service", "action": "restarted", "ok": true }
```

### POST /deploy/{service}
Performs a full deployment: `git fetch`, `git pull`, then `systemctl restart`.

**Success response:**
```json
{
  "service": "openclaw-agent.service",
  "action": "deployed",
  "success": true,
  "repo_path": "/opt/openclaw-agent",
  "commit_before": "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0",
  "commit_after": "b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0a1",
  "fetch_output": "From https://github.com/...\n   a1b2c3d..b2c3d4e  main -> origin/main",
  "pull_output": "Updating a1b2c3d..b2c3d4e\nFast-forward\n agent.py | 5 +++--\n 1 file changed, 3 insertions(+), 2 deletions(-)",
  "restart_result": {
    "success": true,
    "stdout": "",
    "stderr": ""
  },
  "status_summary": {
    "active": true,
    "state": "active"
  },
  "log_tail": [
    "2026-05-06T09:00:01+0000 srv1 openclaw-agent[1234]: Starting...",
    "2026-05-06T09:00:02+0000 srv1 openclaw-agent[1234]: Ready"
  ],
  "ok": true
}
```

**When no update is available (commit hashes match):**
```json
{
  "service": "openclaw-agent.service",
  "action": "deployed",
  "success": true,
  "repo_path": "/opt/openclaw-agent",
  "commit_before": "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0",
  "commit_after": "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0",
  "fetch_output": "",
  "pull_output": "Already up to date.",
  "restart_result": {
    "success": true,
    "stdout": "",
    "stderr": ""
  },
  "status_summary": {
    "active": true,
    "state": "active"
  },
  "log_tail": [...],
  "ok": true
}
```

### Errors
```json
{ "detail": "Service 'bad.service' is not in the allowed list" }   // 400
{ "detail": "Unauthorized" }                                        // 401
{ "detail": "Restart failed: <stderr>" }                           // 500
```

---

## Example calls (from Link)

```
GET /health
→ { "status": "ok" }

GET /status/openclaw-agent.service
Authorization: Bearer ••••
→ { "service": "openclaw-agent.service", "active": true, "state": "active" }

GET /logs/openclaw-crypto.service?n=30
Authorization: Bearer ••••
→ { "service": "...", "lines": [...], "returncode": 0 }

POST /restart/openclaw-agent.service
Authorization: Bearer ••••
→ { "service": "...", "action": "restarted", "ok": true }

POST /deploy/openclaw-agent.service
Authorization: Bearer ••••
→ {
  "service": "openclaw-agent.service",
  "action": "deployed",
  "success": true,
  "repo_path": "/opt/openclaw-agent",
  "commit_before": "abc123...",
  "commit_after": "def456...",
  "fetch_output": "...",
  "pull_output": "Already up to date.",
  "restart_result": {"success": true, "stdout": "", "stderr": ""},
  "status_summary": {"active": true, "state": "active"},
  "log_tail": [...],
  "ok": true
}
```

---

## Source

Code lives in `vps-control-api/` in this repository (`leeheggan-droid/openclaw-control`).
The service runs from `/opt/openclaw-control-api/` on the VPS.

---

*Added: 2026-05-06*
