# Service: Alpaca ORB Bite Bot

> Persistent memory for the `alpaca_orb_bite_bot` systemd service.
> For the full host context (all bots), see `link/context/services/host-overview.md`.

---

## Service Identity

| Field             | Value                          |
|-------------------|--------------------------------|
| systemd unit      | `alpaca_orb_bite_bot.service`  |
| Execution model   | systemd-managed (native host)  |
| Runtime           | Likely Python — confirm via `systemctl cat alpaca_orb_bite_bot.service` |
| VPS               | `srv1501082` / `72.61.123.4`   |
| VPS user          | `jacks`                        |
| Logs              | journald only                  |

---

## Authoritative Commands

### Check status (safe — run freely)

```bash
systemctl status alpaca_orb_bite_bot.service
```

### Stream live logs (safe — run freely)

```bash
journalctl -u alpaca_orb_bite_bot.service -f
```

### Inspect the service unit file (safe — run freely)

```bash
systemctl cat alpaca_orb_bite_bot.service
```

### Restart (confirm intent before running)

```bash
systemctl restart alpaca_orb_bite_bot.service
```

### Stop ⚠️ destructive — require explicit user confirmation

```bash
systemctl stop alpaca_orb_bite_bot.service
```

### Start

```bash
systemctl start alpaca_orb_bite_bot.service
```

---

## Notes

- This is a "first-class" systemd service — same mental model as `openclaw-crypto.service`.
- Logs are in journald, **not** Docker.
- Confirm exact entrypoint and working directory by running `systemctl cat alpaca_orb_bite_bot.service`.
- `stop` and any action that interrupts a running trade should require explicit user confirmation.

---

## Troubleshooting — Is the bot duplicated or misbehaving?

### Step 1 — Verify there is exactly one systemd unit

Run these three commands **before** concluding there is a duplicate process:

```bash
# 1. Check the service unit directly
systemctl status alpaca_orb_bite_bot.service

# 2. List all running systemd services and filter for this bot
systemctl list-units --type=service | grep alpaca

# 3. Confirm there is only one process
ps -ef | grep alpaca_orb_bite_bot | grep -v grep
```

Expected results when healthy:
- `systemctl status` shows **one** active unit, `active (running)`.
- `systemctl list-units` shows **one** matching row.
- `ps -ef` shows **one** Python process (`main.py` or equivalent).

If all three return a single instance, the service is **not** duplicated.

### Step 2 — Distinguish process duplication from config duplication

These are two different classes of problem:

| Problem class | Symptom | Investigation |
|---|---|---|
| **Duplicate process** | Two or more Python processes running the same bot | `ps -ef \| grep alpaca` shows > 1 process |
| **Duplicate config source** | Auth errors, wrong credentials, unexpected behaviour | Multiple files supplying the same env var |

A bot can run as a **single process** and still have **duplicate credential sources**.
Removing the systemd unit or restarting does not fix a credential conflict — the
sources must be reconciled first.

### Step 3 — Reconcile credential sources

For `alpaca_orb_bite_bot`, the authoritative credential source is the systemd
`EnvironmentFile` (typically `/etc/alpaca_orb_bite_bot.env`).  Common sources of
conflict include:

- A repo-local `.env` file loaded by the app at runtime
- A repo-local `.alpaca_keys` file loaded by the app at runtime
- Hardcoded fallback values inside the Python script itself

Check which sources exist:

```bash
# Inspect what the systemd unit declares as its EnvironmentFile
systemctl cat alpaca_orb_bite_bot.service | grep -i environmentfile

# Check for repo-local credential files
ls -la /home/jacks/alpaca_orb_bite_bot/.env \
        /home/jacks/alpaca_orb_bite_bot/.alpaca_keys 2>/dev/null
```

**Resolution rule:** if the bot runs under systemd, `/etc/alpaca_orb_bite_bot.env`
should be the **only** source of truth for credentials.  Rename or remove repo-local
files (`.env`, `.alpaca_keys`) once you have confirmed the systemd env file is
complete.  Always restart and check logs after doing so:

```bash
sudo systemctl restart alpaca_orb_bite_bot.service
journalctl -u alpaca_orb_bite_bot.service -n 30 --no-pager
```

### TL;DR decision tree

```
Bot behaving unexpectedly?
│
├─ Is more than one process running?  (ps -ef | grep alpaca)
│   ├─ YES → kill the stale process; investigate why it escaped systemd
│   └─ NO  → it is NOT a duplicate-process problem; continue below
│
└─ Is the service running but producing auth/logic errors?
    └─ Check for duplicate credential files (.env, .alpaca_keys vs EnvironmentFile)
        ├─ Multiple sources found → reconcile; make systemd EnvironmentFile the only source
        └─ Single source found    → the error is application-level; trace the failing request
```

---

*Last updated: 2026-05-17*
