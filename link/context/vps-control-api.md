# VPS Control API

> Direct HTTP API running on the VPS at port **8765**. Provides sub-second
> status checks, log fetches, restarts, and deploys — without waiting 30–90 s
> for a GitHub Actions runner.

---

## Current Status

| Component | Status | Confirmed |
|-----------|--------|-----------|
| `openclaw-control-api.service` on VPS | ✅ Deployed and running at `72.61.123.4:8765` | 2026-05-06 — service file deployed by Copilot |
| `VPS_CONTROL_API_URL` in Vercel | ⬜ Requires operator action | Set to `http://72.61.123.4:8765` in Vercel project settings |
| `VPS_CONTROL_API_KEY` in Vercel | ⬜ Requires operator action | Must match the `VPS_CONTROL_API_KEY` value in `/etc/openclaw-control-api.env` on VPS |
| `VPS_CONTROL_API_KEY` GitHub Secret | ⬜ Requires operator action | Needed for `verify-vps-api.yml` to authenticate its status check |
| End-to-end proof (workflow run) | ⬜ Run `verify-vps-api.yml` after merge | See **Verifying API is up** section below |

> **The API is deployed and the VPS service is running.** Link will use it as the primary
> control path once `VPS_CONTROL_API_URL` and `VPS_CONTROL_API_KEY` are set in Vercel.
> Until then, Link falls back to GitHub Actions (see `how-link-interacts.md`).

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

`state` is always one of `"active"`, `"inactive"`, `"failed"`, or `"unknown"`.
`raw_status` is included when the raw `systemctl is-active` output differs from
the normalised state (e.g. `"activating"` → `state: "active"`, `raw_status: "activating"`).

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

Only services with a verified git repo on the VPS are deployable:

| Service | Deployable | Notes |
|---|---|---|
| `openclaw-agent.service` | ✅ | `/opt/openclaw-agent` |
| `openclaw-crypto.service` | ✅ | `/home/jacks/openclaw-crypto` — check positions before deploying |
| `openclaw-vibe-gateway.service` | ❌ | wraps an external Docker image; no local repo |
| `alpaca_orb_bite_bot.service` | ❌ | path unverified — enable once confirmed |
| `linkedin-news.timer/service` | ❌ | no standalone git repo |

**Success response:**
```json
{
  "service": "openclaw-agent.service",
  "action": "deployed",
  "success": true,
  "repo_path": "/opt/openclaw-agent",
  "commit_before": "a1b2c3d4e5f6...",
  "commit_after": "b2c3d4e5f6g7...",
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
  "log_error": null,
  "ok": true
}
```

`status_summary.state` is always one of `"active"`, `"inactive"`, `"failed"`, or `"unknown"`.
`raw_status` is included in `status_summary` only when the raw output differs from the normalised state.
`log_error` is `null` on success; contains an error string when log collection fails (deploy still succeeds).

**When no update is available (commit hashes match):**
```json
{
  "service": "openclaw-agent.service",
  "action": "deployed",
  "success": true,
  "repo_path": "/opt/openclaw-agent",
  "commit_before": "a1b2c3d4e5f6...",
  "commit_after": "a1b2c3d4e5f6...",
  "fetch_output": "",
  "pull_output": "Already up to date.",
  "restart_result": { "success": true, "stdout": "", "stderr": "" },
  "status_summary": { "active": true, "state": "active" },
  "log_tail": ["..."],
  "log_error": null,
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
  "log_error": null,
  "ok": true
}
```

---

## Fallback Behavior

If the VPS Control API is unreachable, Link must:

1. **Report the failure honestly** — tell the user that the direct API could not
   be reached (include the error: connection refused, timeout, or 401).
2. **Offer the GitHub Actions fallback** — Link can trigger `link.yml` via
   `workflow_dispatch` instead.  This adds 30–90 s of latency but does not
   require the API to be running.
3. **Do not silently swallow errors** — never return a fabricated "ok" status
   when the API returned an error or was unreachable.

### Fallback trigger table

| API response             | Meaning                          | Link action                                         |
|--------------------------|----------------------------------|-----------------------------------------------------|
| Connection refused / timeout | `openclaw-control-api.service` is down | Use GitHub Actions fallback; alert operator |
| `401 Unauthorized`       | Key mismatch                     | Alert operator to rotate key; use fallback          |
| `400 Bad Request`        | Service name not in allow-list   | Fix service name — no fallback needed               |
| `500 Internal Server Error` | systemctl/journalctl failed   | Report error; optionally use fallback               |

### Verifying API is up

Run the **"Verify VPS Control API"** workflow (`verify-vps-api.yml`) from the
GitHub Actions tab.  A passing run confirms the API is reachable and that the
`VPS_CONTROL_API_KEY` in GitHub Secrets matches the key on the VPS.

Alternatively, from any machine with network access to the VPS:

```bash
# Unauthenticated liveness probe
curl http://72.61.123.4:8765/health

# Authenticated status check
curl -H "Authorization: Bearer <key>" \
     http://72.61.123.4:8765/status/openclaw-agent.service
```

---

## Source

Code lives in `vps-control-api/` in this repository (`leeheggan-droid/openclaw-control`).
The service runs from `/opt/openclaw-control-api/` on the VPS.

Systemd unit: `openclaw-control-api.service`
Env file: `/etc/openclaw-control-api.env` (contains `VPS_CONTROL_API_KEY`)

---

*Added: 2026-05-06 | Fallback section added: 2026-05-06*
