# API Quirks & Technical Debt

> Platform-specific behaviours, gotchas, and workarounds discovered during integration work.

---

## GitHub API

### workflow_dispatch input must be `action` (not `task`)
**Discovered:** 2026-05-05
**Issue:** The workflow input is named `action`. If Link sends `task` the input is silently ignored and the default (`status-all`) runs instead.
**Workaround:** Always use `"inputs": { "action": "<value>" }` in the dispatch payload. See `environment.md` for all valid values.
**Status:** Fixed in link.yml (2026-05-05)

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

## Technical Debt

*(None logged yet)*

---

## Template

### [Specific Behaviour]
**Discovered:** YYYY-MM-DD
**Issue:** [What happens]
**Workaround:** [How to handle it]
**Status:** Active / Resolved / Obsolete
