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

*Last updated: 2026-05-03*
