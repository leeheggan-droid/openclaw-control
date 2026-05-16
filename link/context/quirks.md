# API Quirks & Technical Debt

> Platform-specific behaviours, gotchas, and workarounds discovered during integration work.

---

## GitHub API

### workflow_dispatch canonical input is `action`; `task` is legacy-compatible
**Discovered:** 2026-05-05
**Issue:** Fallback dispatches previously broke when callers sent only `task`.
**Workaround:** Prefer `"inputs": { "action": "<value>" }`; `task` is accepted as a compatibility alias in `link.yml`.
**Status:** Active (canonical `action` retained; alias support added)

### `systemd-stop` / `systemd-start` require explicit `service`
**Discovered:** 2026-05-05
**Issue:** Both actions validate that `service` is explicitly provided (not the default `crypto-bot`). Omitting it returns an Ansible failure — this is intentional to prevent accidental stops.
**Workaround:** Always pass the exact unit name, e.g. `alpaca_orb_bite_bot.service`. See the service name table in `environment.md`.
**Status:** Active (by design)

---

## Kraken API

*(None logged yet)*

---

## Alpaca API

*(None logged yet)*

---

## Telegram Bot API

*(None logged yet)*

---

## VPS Control API

### Link must use `VPS_CONTROL_API_URL` + `VPS_CONTROL_API_KEY` (not GitHub Actions) as primary path
**Discovered:** 2026-05-06
**Issue:** `environment.md` sets context to `VPS_CONTROL_API`. If Link falls back to GitHub Actions for every request the response is 30–90 s slower and the API monitoring gap goes unnoticed.
**Workaround:** Always attempt the direct API first. Only use GitHub Actions if the API returns a connection error, timeout, or 401. Tell the user when the fallback is used.
**Status:** Active (by design)

### `/health` is unauthenticated; all other endpoints require `Authorization: Bearer`
**Discovered:** 2026-05-06
**Issue:** Omitting the `Authorization` header on any endpoint other than `/health` returns `403 Forbidden` (FastAPI security auto-error), not `401`. This can look like a configuration error.
**Workaround:** Always include `Authorization: Bearer <VPS_CONTROL_API_KEY>` on `/status`, `/logs`, `/restart`, and `/deploy`. Use `/health` as a no-auth liveness probe before attempting authenticated calls.
**Status:** Active (by design)

### `VPS_CONTROL_API_KEY` must be set in both Vercel and GitHub Secrets
**Discovered:** 2026-05-06
**Issue:** The `verify-vps-api.yml` workflow reads `VPS_CONTROL_API_KEY` from GitHub Secrets. Link reads it from Vercel env vars. If only one is set, the other path silently fails.
**Workaround:** Ensure the same key value is stored as `VPS_CONTROL_API_KEY` in both Vercel project settings and as a GitHub repository secret.
**Status:** Active — operator must keep both in sync when rotating the key

---

## Technical Debt

*(None logged yet)*

---

## Template

### [Specific Behaviour]
**Discovered:** YYYY-MM-DD
**Issue:** [What happens]
**Workaround:** [How to handle it]
**Status:** Active / Resolved / Obsolete
