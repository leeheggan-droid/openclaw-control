# Link ↔ Control Contract Integration Review

> Review of how the contract-driven VPS Control API now interacts with Link, what
> Link must change, and what configuration each side needs.

---

## Executive summary

`openclaw-control` is now the source of truth for operational capabilities. Link
should stop assuming service names, actions, confirmation rules, and output
shapes from markdown alone, and should instead discover them from the live VPS
Control API contract.

**New manager flow for Link:**

1. `GET /health`
2. `GET /contract`
3. decide whether the requested action is allowed
4. ask the user for confirmation when policy requires it
5. `POST /jobs`
6. render the structured result
7. fall back to GitHub Actions only if the API is unavailable or unauthorised

---

## What changed in control

The VPS Control API now exposes:

- a versioned control contract (`/contract`)
- discovery endpoints (`/capabilities`, `/services`, `/actions`, `/operators`)
- a bounded execution surface (`/jobs`, `/jobs/{job_id}`)
- centralized confirmation policy
- standardized diagnostics output

Legacy endpoints still exist:

- `GET /status/{service}`
- `GET /logs/{service}`
- `GET /diagnostics/{service}`
- `POST /restart/{service}`
- `POST /deploy/{service}`

These remain as compatibility paths while Link migrates.

---

## How Link should interact with control now

### 1. Treat control as the source of truth

Link should no longer hard-code:

- service names
- which actions exist
- which action requires confirmation
- whether a service is deployable
- operator ownership

Instead, Link should read `/contract` at session start or before the first
operational action.

### 2. Fail safe on unknown contract versions

Link should compare the returned `contract_version` with the versions it knows
how to handle.

If the version is unknown:

- do **not** guess
- tell the user the control contract version is newer than Link expects
- avoid sending risky actions until Link is updated

### 3. Prefer `/jobs` for execution

Link should submit intent through:

```json
{
  "action": "diagnostics",
  "service": "openclaw-agent.service",
  "parameters": { "n": 20 }
}
```

For risky actions:

```json
{
  "action": "deploy",
  "service": "openclaw-crypto.service",
  "confirmed": true,
  "confirmation_note": "Confirmed no open Kraken positions before deploy."
}
```

### 4. Handle policy failures as product behavior, not API bugs

If control returns:

- `error_code: "confirmation_required"`

Link should:

- explain what confirmation is missing
- ask the user explicitly
- include the confirmation in a new `/jobs` request

### 5. Keep GitHub Actions only as fallback

Link should keep the existing `link.yml` fallback for:

- connection refused
- timeout
- invalid/missing API key

Link should **not** use GitHub Actions as the default path when the direct API
is healthy.

---

## Impact on Link architecture

### Good news

The new control API simplifies Link:

- less prompt-specific operational knowledge
- fewer hard-coded service/action mappings
- clearer confirmation boundaries
- more predictable output shape

### Required Link changes

Link still needs product code changes to take full advantage of the contract:

1. add a contract-fetch step before action execution
2. cache or validate `contract_version`
3. route operational actions to `/jobs`
4. recognize `confirmation_required`
5. keep the GitHub Actions fallback path
6. update any hard-coded control prompts/context that still assume the old flow

---

## Environment variables — what each side needs

### Control API environment

**Required today:**

| Variable | Required | Why |
|---|---|---|
| `VPS_CONTROL_API_KEY` | Yes | Authenticates all control API calls except `/health` |

**Not required today by control code:**

- `GROQ_API_KEY`
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- any other LLM provider key

Even though the contract mentions `groq-optional` for some operator roles, the
current implementation is deterministic and does **not** call an LLM.

**Optional future variables only if control later gains LLM-backed operators:**

- `GROQ_API_KEY`
- provider/model selection variables
- operator-specific feature flags

### Link (Vercel) environment

| Variable | Required | Why |
|---|---|---|
| `VPS_CONTROL_API_URL` | Yes | Base URL for direct control calls |
| `VPS_CONTROL_API_KEY` | Yes | Auth header for control API |
| GitHub token with Actions write | Yes for fallback | Trigger `link.yml` when direct API is unavailable |

### GitHub repository secrets

| Secret | Required | Why |
|---|---|---|
| `VPS_CONTROL_API_KEY` | Yes | Used by `verify-vps-api.yml` |
| `VPS_SSH_KEY` | Yes for fallback | GitHub Actions fallback |
| `ANSIBLE_INVENTORY` | Yes for fallback | GitHub Actions fallback |

---

## Setup recommendation

For control, use an environment file at:

```text
/etc/openclaw-control-api.env
```

This repo now includes an example template:

```text
vps-control-api/openclaw-control-api.env.example
```

Copy it to the VPS, set a strong `VPS_CONTROL_API_KEY`, and restart:

```bash
sudo systemctl restart openclaw-control-api.service
```

---

## Recommended rollout for Link

### Phase 1 — safe migration

- keep existing fallback behavior
- add `GET /contract`
- begin validating service/action names against the contract

### Phase 2 — contract-first execution

- switch operational requests to `/jobs`
- surface structured results directly to the user

### Phase 3 — manager-only behavior

- remove hard-coded operational assumptions from Link prompts/context
- let control own policy and capability truth

---

## Files to use next

| File | Purpose |
|---|---|
| `link/context/link-contract-migration-prompt.md` | Prompt for Copilot to update the Link repo |
| `vps-control-api/openclaw-control-api.env.example` | Example setup file for control API env |
| `link/context/vps-control-api.md` | API reference for humans |
| `link/context/how-link-interacts.md` | End-to-end flow overview |

---

*Added: 2026-05-16*
