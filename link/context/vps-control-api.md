# VPS Control API

> Direct HTTP API running on the VPS at port **8765**. It now acts as the
> machine-readable **control room** contract for Link: discovery, bounded
> operator execution, diagnostics, and legacy direct service actions all live
> here.

---

## Current Status

| Component | Status | Confirmed |
|-----------|--------|-----------|
| `openclaw-control-api.service` on VPS | ✅ Deployed and running at `72.61.123.4:8765` | 2026-05-06 — service file deployed by Copilot |
| Versioned control contract | ✅ Present in `vps-control-api/control_contract.json` | 2026-05-16 |
| Discovery endpoints | ✅ `/contract`, `/capabilities`, `/actions`, `/services`, `/operators` | 2026-05-16 |
| Job execution endpoint | ✅ `/jobs` with in-memory job ledger | 2026-05-16 |
| `VPS_CONTROL_API_URL` in Vercel | ⬜ Requires operator action | Set to `http://72.61.123.4:8765` in Vercel project settings |
| `VPS_CONTROL_API_KEY` in Vercel | ⬜ Requires operator action | Must match the `VPS_CONTROL_API_KEY` value in `/etc/openclaw-control-api.env` on VPS |
| `VPS_CONTROL_API_KEY` GitHub Secret | ⬜ Requires operator action | Needed for `verify-vps-api.yml` to authenticate its status check |

> **The API is deployed and the contract exists in this repo, but production
> Link still needs the Vercel/GitHub secret wiring above completed.** Once that
> is done, Link should read `/contract` or `/capabilities` before acting so
> control, policy, and service metadata come from one source.

> **Migration note:** existing integrations can keep using `/status`, `/logs`,
> `/restart`, and `/deploy` while Link moves to the `/contract` + `/jobs` flow.

---

## Base URL

```
http://72.61.123.4:8765
```

## Auth

Every request except `/health` requires:

```
Authorization: Bearer <VPS_CONTROL_API_KEY>
```

The key is stored in Vercel as `VPS_CONTROL_API_KEY`.

---

## Source of Truth

Machine-readable control contract:

```
vps-control-api/control_contract.json
```

This contract defines:

- manager role (`Link` as the control room manager)
- junior operator roles
- allowed services
- allowed actions
- policy metadata
- confirmation rules
- compatibility notes and contract version

The API serves this same contract via `/contract`.

---

## Endpoints

### Discovery and introspection

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Unauthenticated liveness probe |
| `GET` | `/contract` | Full machine-readable control contract |
| `GET` | `/capabilities` | High-level summary of services, actions, operators, and policies |
| `GET` | `/services` | Allowed service list + metadata |
| `GET` | `/actions` | Allowed action list + metadata |
| `GET` | `/operators` | Manager + junior operator definitions |

### Direct read / control actions

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/status` | Status summary for all allowed services |
| `GET` | `/status/{service}` | `systemctl is-active {service}` |
| `GET` | `/logs/{service}?n=50` | Last N lines of journald logs |
| `GET` | `/diagnostics/{service}?n=20` | Standardized status + logs bundle |
| `POST` | `/restart/{service}` | Legacy direct restart endpoint |
| `POST` | `/deploy/{service}` | Legacy direct deploy endpoint |

### Unified bounded execution

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/jobs` | Submit a bounded action (`status-all`, `status`, `logs`, `diagnostics`, `restart`, `deploy`) |
| `GET` | `/jobs/{job_id}` | Read a previously submitted in-memory job record |

> New Link integrations should prefer `/contract` + `/jobs`. Legacy endpoints
> remain available for compatibility.

---

## Manager and operator model

| Role | Purpose |
|------|---------|
| `link-manager` | Clarifies intent, reads the contract, requests confirmation, delegates, and explains results |
| `read-only-operator` | Bounded status/log/diagnostics tasks; can be backed by cheap LLM routing or deterministic commands |
| `service-control-operator` | Restart tasks with explicit confirmation |
| `deploy-diagnostics-operator` | Deploy + post-action diagnostics bundles |

The operator model is declared in the contract even when execution is currently
deterministic. That lets Link reason about delegation without hard-coding the
shape of the control room.

---

## Allowed service names

```
openclaw-agent.service
openclaw-crypto.service
openclaw-vibe-gateway.service
alpaca_orb_bite_bot.service
linkedin-news.timer
linkedin-news.service
```

---

## Policy enforcement

### Confirmation rules

- `status`, `logs`, and `diagnostics` are read-only and do **not** require confirmation.
- `restart` and `deploy` are policy-gated in `/jobs` and require `confirmed: true`.
- Money-risk services such as `openclaw-crypto.service` also require a
  `confirmation_note` recording the manual safety check.

### Example blocked job

```json
{
  "status": "failed",
  "error_code": "confirmation_required",
  "result": {
    "ok": false,
    "summary": "restart blocked by control policy",
    "reason": "restart on openclaw-agent.service requires explicit confirmation in control"
  }
}
```

---

## Response shapes

### GET /contract

```json
{
  "contract_version": "2026-05-16.1",
  "api_version": "2.0.0",
  "manager": { "id": "link-manager", "role": "control-room-manager" },
  "operators": [{ "id": "read-only-operator" }],
  "services": [{ "id": "openclaw-agent.service" }],
  "actions": [{ "id": "status" }],
  "policies": { "confirmation": { "required_for_actions": ["restart", "deploy"] } }
}
```

### GET /diagnostics/{service}

```json
{
  "ok": true,
  "status": "succeeded",
  "action": "diagnostics",
  "service": "openclaw-agent.service",
  "summary": "GitHub Agent diagnostics collected",
  "reason": null,
  "operator": "read-only-operator",
  "data": {
    "service": "openclaw-agent.service",
    "status_summary": { "active": true, "state": "active" },
    "log_line_count": 20,
    "log_returncode": 0
  },
  "artifacts": {
    "status_summary": { "active": true, "state": "active" },
    "log_lines": ["..."]
  }
}
```

### POST /jobs

Request:

```json
{
  "action": "deploy",
  "service": "openclaw-agent.service",
  "confirmed": true,
  "confirmation_note": "Operator confirmed deploy."
}
```

Response:

```json
{
  "id": "4e6486ef-2f1a-4aa2-95be-4c4f2d4fcf37",
  "status": "succeeded",
  "action": "deploy",
  "service": "openclaw-agent.service",
  "operator": "deploy-diagnostics-operator",
  "parameters": {},
  "submitted_at": "2026-05-16T02:50:00+00:00",
  "started_at": "2026-05-16T02:50:00+00:00",
  "completed_at": "2026-05-16T02:50:03+00:00",
  "confirmed": true,
  "confirmation_note": "Operator confirmed deploy.",
  "result": {
    "ok": true,
    "status": "succeeded",
    "action": "deploy",
    "service": "openclaw-agent.service",
    "summary": "Deployed GitHub Agent"
  }
}
```

### Legacy direct endpoints

`/status`, `/logs`, `/restart`, and `/deploy` remain live so current clients do
not break while Link moves to the contract-driven flow.

---

## Fallback behavior

If the VPS Control API is unreachable, Link must:

1. **Report the failure honestly** — include the API error.
2. **Offer the GitHub Actions fallback** — `link.yml` remains available.
3. **Do not fabricate success** — return the real API or fallback outcome.

### Fallback trigger table

| API response | Meaning | Link action |
|--------------|---------|------------|
| Connection refused / timeout | `openclaw-control-api.service` is down | Use GitHub Actions fallback; alert operator |
| `401 Unauthorized` | Key mismatch | Alert operator to rotate key; use fallback |
| `400 Bad Request` | Unknown service/action/parameter | Fix request — no fallback needed |
| `500 Internal Server Error` | systemctl/journalctl/git failed | Report error; optionally use fallback |

---

## Verifying API is up

Run the **"Verify VPS Control API"** workflow (`verify-vps-api.yml`) from the
GitHub Actions tab.

Or from any machine with network access to the VPS:

```bash
curl http://72.61.123.4:8765/health

curl -H "Authorization: Bearer <key>" \
     http://72.61.123.4:8765/contract
```

---

## Source

Code lives in `vps-control-api/` in this repository (`leeheggan-droid/openclaw-control`).
The service runs from `/opt/openclaw-control-api/` on the VPS.

Systemd unit: `openclaw-control-api.service`
Env file: `/etc/openclaw-control-api.env` (contains `VPS_CONTROL_API_KEY`)

---

*Added: 2026-05-06 | Control contract update: 2026-05-16*
