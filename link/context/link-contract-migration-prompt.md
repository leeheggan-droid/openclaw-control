# Copilot Prompt — Update Link for the Control Contract

Use this prompt in the **Link** repository to migrate Link to the new
contract-driven control API:

---

You are updating Link so it understands the new contract-driven VPS Control API
in `openclaw-control`.

## Goal

Make Link behave as the **control room manager**:

- discover control capabilities from the live contract
- ask for confirmation when required
- delegate bounded actions through `/jobs`
- fall back to GitHub Actions only when the direct API is unavailable

## Source of truth in `openclaw-control`

- Contract endpoint: `GET /contract`
- Discovery endpoints:
  - `GET /capabilities`
  - `GET /services`
  - `GET /actions`
  - `GET /operators`
- Execution endpoints:
  - `POST /jobs`
  - `GET /jobs/{job_id}`
- Legacy compatibility endpoints still exist, but new Link code should prefer
  the contract + jobs flow

## Required Link behavior

1. **Read `/health` first**
   - If healthy, proceed to `/contract`
   - If unavailable, fall back to GitHub Actions and tell the user

2. **Read `/contract` before operational actions**
   - Use it to discover:
     - valid services
     - valid actions
     - confirmation rules
     - contract version

3. **Fail safe on unknown `contract_version`**
   - If Link does not recognize the contract version, avoid risky execution
   - Tell the user Link needs updating for the new control contract

4. **Use `POST /jobs` for execution**
   - `status`
   - `logs`
   - `diagnostics`
   - `restart`
   - `deploy`

5. **Handle confirmation requirements**
   - If `/jobs` returns `error_code: "confirmation_required"`, ask the user
   - Re-submit with:
     - `confirmed: true`
     - `confirmation_note` where required

6. **Respect money-risk policy**
   - For `openclaw-crypto.service`, require explicit confirmation and a note that
     manual position checks were completed before non-read actions

7. **Keep GitHub Actions as fallback only**
   - Continue supporting `link.yml` fallback if:
     - API is unreachable
     - API times out
     - API returns auth failure

## Environment variables Link needs

- `VPS_CONTROL_API_URL`
- `VPS_CONTROL_API_KEY`
- GitHub token with Actions write / `workflow` scope for fallback

## Control environment note

The current control API does **not** need an LLM provider key in its env today.
It only requires `VPS_CONTROL_API_KEY`. The operator roles are contract-defined,
but current execution is deterministic.

## Deliverables in Link repo

- Update the control client to fetch `/contract`
- Update execution flow to use `/jobs`
- Add contract-version safety checks
- Preserve GitHub Actions fallback
- Update any hard-coded prompt/context assumptions about service names, actions,
  or confirmation rules
- Add tests for:
  - unknown contract version
  - `confirmation_required`
  - successful read-only `/jobs` action
  - fallback when direct API is down

## Reference docs in `openclaw-control`

- `link/context/link-control-contract-review.md`
- `link/context/how-link-interacts.md`
- `link/context/vps-control-api.md`
- `vps-control-api/control_contract.json`

Make the smallest practical Link changes that complete this migration cleanly.
