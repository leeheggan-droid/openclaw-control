# Service: openclaw-crypto

> Persistent memory for the `openclaw-crypto.service` systemd unit.
> For the full host context (all bots), see `link/context/services/host-overview.md`.

---

## Service Identity

| Field              | Value                              |
|--------------------|------------------------------------|
| systemd unit       | `openclaw-crypto.service`          |
| Execution model    | **systemd-managed, native host — NOT Docker** |
| Entrypoint         | `/usr/bin/python3 main.py`         |
| Working directory  | `/home/jacks/openclaw-crypto`      |
| Secrets            | `.env.secrets` (in working dir)    |
| VPS                | `srv1501082` / `72.61.123.4`       |
| VPS user           | `jacks`                            |
| Logs               | journald only                      |

---

## Authoritative Commands

### Check status (safe — run freely)

```bash
systemctl status openclaw-crypto.service
```

### Stream live logs (safe — run freely)

```bash
journalctl -u openclaw-crypto.service -f
```

### Inspect the service unit file (safe — run freely)

```bash
systemctl cat openclaw-crypto.service
```

### Restart (confirm intent before running)

```bash
systemctl restart openclaw-crypto.service
```

### Stop ⚠️ destructive — require explicit user confirmation

```bash
systemctl stop openclaw-crypto.service
```

### Start

```bash
systemctl start openclaw-crypto.service
```

---

## Critical Notes

> ⚠️ **This bot does NOT run in Docker.** Never use `docker ps` or `docker logs`
> to check its state — they will show nothing and give a false "not running" result.

> Old file-based logs under `/data/.openclaw/...` are historical only — not
> authoritative for current state.

- All authoritative log output goes to journald (`journalctl`).
- Secrets are loaded from `.env.secrets` in the working directory — never commit
  this file.

---

*Last updated: 2026-05-03*
