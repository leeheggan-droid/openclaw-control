# How Link Interacts With openclaw-control

> This file documents the end-to-end control-room flow between Link (the Vercel
> AI assistant at www.leeheggan.tech) and `openclaw-control`.

---

## Overview

Link should now behave as the **control room manager**, not the low-level
executor. `openclaw-control` is the source of truth for:

- services
- actions
- operator roles
- confirmation rules
- compatibility notes

That source of truth lives in `vps-control-api/control_contract.json` and is
served live by the VPS Control API.

---

## Primary Path — Contract-first VPS Control API

```
┌──────────────────────────────────────────────┐
│  Link (manager)                             │
│                                              │
│  1. Read /contract or /capabilities          │
│  2. Decide which bounded operator to use     │
│  3. Ask for confirmation if policy requires  │
│  4. Submit /jobs request                     │
└───────────────────┬──────────────────────────┘
                    │ HTTP
                    ▼
┌──────────────────────────────────────────────┐
│  VPS Control API                             │
│                                              │
│  - validates auth                            │
│  - validates action + service against        │
│    contract                                  │
│  - enforces confirmation policy              │
│  - executes bounded operator task            │
│  - returns structured result                 │
└───────────────────┬──────────────────────────┘
                    │ JSON
                    ▼
┌──────────────────────────────────────────────┐
│  Link (manager)                             │
│                                              │
│  - explains result to the user               │
│  - asks follow-up questions if needed        │
│  - falls back to GitHub Actions if API down  │
└──────────────────────────────────────────────┘
```

---

## Manager + junior operator model

| Role | Runs where | Responsibility |
|------|------------|----------------|
| Link manager | Vercel | Intent clarification, confirmation, delegation, user-facing explanation |
| Read-only operator | Control API | Status, logs, diagnostics |
| Service control operator | Control API | Restart |
| Deploy and diagnostics operator | Control API | Deploy + verification bundle |

The operator layer is currently contract-driven and deterministic. The contract
still leaves room for cheap LLM-backed bounded operators later, without making
Link re-learn control behavior from markdown.

---

## Required Vercel environment variables

| Variable | Description | Status |
|----------|-------------|--------|
| `VPS_CONTROL_API_URL` | `http://72.61.123.4:8765` | Must be set in Vercel project settings |
| `VPS_CONTROL_API_KEY` | Shared secret matching `/etc/openclaw-control-api.env` | Must be set in Vercel project settings |

If either is missing or wrong, Link must fall back to GitHub Actions.

---

## Discovery-first call sequence

### 1. Read the contract

```
GET /contract
Authorization: Bearer <VPS_CONTROL_API_KEY>
```

Link should use this to discover:

- current `contract_version`
- valid service names
- valid actions
- which operator owns each action
- whether confirmation is required
- compatibility notes

### 2. Optional capability refresh

```
GET /capabilities
GET /services
GET /actions
GET /operators
```

These are convenience reads if Link wants narrower payloads.

### 3. Execute a bounded task

```
POST /jobs
Authorization: Bearer <VPS_CONTROL_API_KEY>
Content-Type: application/json

{
  "action": "diagnostics",
  "service": "openclaw-agent.service",
  "parameters": { "n": 20 }
}
```

### 4. Read the result

`POST /jobs` returns the completed job record immediately for current bounded
operations. Link can also fetch it later:

```
GET /jobs/<job_id>
```

---

## Confirmation flow

Control policy now lives in control, not just in Link prompts.

### Example: restart

```json
{
  "action": "restart",
  "service": "openclaw-agent.service",
  "confirmed": true,
  "confirmation_note": "User explicitly approved restart."
}
```

### Extra safeguard for money-risk services

For `openclaw-crypto.service`, Link should only submit a risky action after the
manual Kraken position check has been completed and recorded in
`confirmation_note`.

If confirmation is missing, `/jobs` returns a failed job with
`error_code: "confirmation_required"`.

---

## Legacy direct endpoints

The following still exist for compatibility:

| Method | Path |
|--------|------|
| `GET` | `/status/{service}` |
| `GET` | `/logs/{service}` |
| `GET` | `/diagnostics/{service}` |
| `POST` | `/restart/{service}` |
| `POST` | `/deploy/{service}` |

New integrations should prefer the contract + jobs flow because it removes
hard-coded assumptions from Link.

---

## Fallback Path — GitHub Actions

If the VPS Control API is unreachable, Link falls back to the `link.yml`
workflow_dispatch path.

```
POST /repos/leeheggan-droid/openclaw-control/actions/workflows/link.yml/dispatches
{
  "ref": "main",
  "inputs": { "action": "systemd-restart" }
}
```

Use fallback only when the direct API is unavailable or unauthenticated.

---

## GitHub Actions inputs

The canonical workflow input is **`action`**. For compatibility, `task` is also accepted as a legacy alias (prefer `action`).

| Input | Type | Required | Values |
|-------|------|----------|--------|
| `action` | choice | No | `status-all`, `systemd-status`, `systemd-logs`, `systemd-restart`, `systemd-stop`, `systemd-start`, `logs-systemd`, `deploy` |
| `task` | string | No | Legacy alias of `action`; send only for backward compatibility |
| `service` | string | No | Required for `systemd-stop`, `systemd-start`, `logs-systemd`, `deploy` |
| `tail_lines` | string | No | Applies to `systemd-logs` and `logs-systemd` |

---

## Fallback token requirements

| Token type | Required permission / scope |
|------------|-----------------------------|
| Fine-grained PAT | **Actions: Read and Write** on `leeheggan-droid/openclaw-control` |
| Classic PAT | **`workflow`** scope |

---

## Verification

Run the **"Verify VPS Control API"** workflow (`verify-vps-api.yml`) to confirm:

1. `GET /health` works
2. `GET /status/openclaw-agent.service` works

For contract-first integration checks, also verify:

3. `GET /contract` works
4. `POST /jobs` succeeds for a read-only action such as `diagnostics`

---

## Code push / deploy cycle

When code is pushed to a bot repo:

1. Link reads the contract
2. Link checks whether deploy is allowed and whether confirmation is required
3. Link submits `POST /jobs` with `action: "deploy"`
4. Control executes the deploy operator and returns a structured result
5. Link communicates the result to the user

---

*Last updated: 2026-05-16 — rewritten for contract-first manager/operator flow*
