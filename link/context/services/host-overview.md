# OpenClaw Bot Operations Brief — Host Overview

> **Master reference for all bots on `srv1501082` (`72.61.123.4`).**  
> Before checking status, debugging, or retrieving logs for any bot, always
> identify the execution layer first. Never assume a bot is Dockerised just
> because Docker exists on the host.

---

## 1. Key Mental Model

| Layer            | What it manages   | Tools to use                             |
|------------------|-------------------|------------------------------------------|
| **systemd**      | Host processes    | `systemctl`, `journalctl`                |
| **Docker**       | Containers        | `docker ps`, `docker logs`, `docker exec`|
| Containers       | App runtime       | shell + app logs                         |
| Workspace files  | Historical state  | not authoritative                        |

---

## 2. Bot Inventory

| Bot                          | Execution model | Service / container identifier                     |
|------------------------------|-----------------|----------------------------------------------------|
| OpenClaw Crypto              | systemd (native)| `openclaw-crypto.service`                          |
| Alpaca ORB Bite Bot          | systemd (native)| `alpaca_orb_bite_bot.service`                      |
| LinkedIn Data Centre News Bot| Docker          | `linkedin_data_centre_news-linkedin-cron-1`        |

---

## 3. Bot Details

### 3.1 OpenClaw Crypto

**Execution model:** systemd-managed, runs directly on host (NOT Docker), Python entrypoint

| Field              | Value                              |
|--------------------|------------------------------------|
| systemd unit       | `openclaw-crypto.service`          |
| Entrypoint         | `/usr/bin/python3 main.py`         |
| Working directory  | `/home/jacks/openclaw-crypto`      |
| Secrets            | `.env.secrets` (in working dir)    |
| Logs               | journald only                      |

**Authoritative commands:**

```bash
systemctl status openclaw-crypto.service
journalctl -u openclaw-crypto.service -f
systemctl cat openclaw-crypto.service
systemctl restart openclaw-crypto.service   # ⚠️ confirm before running
systemctl stop openclaw-crypto.service      # ⚠️ destructive — confirm first
```

> ⚠️ **Do NOT use `docker ps` for this bot.** It does not run in Docker.  
> Old file-based logs under `/data/.openclaw/...` are historical only — not authoritative.

See `link/context/services/openclaw-crypto.md` for full detail.

---

### 3.2 Alpaca ORB Bite Bot

**Execution model:** systemd-managed, long-running production bot (likely Python — confirm via service file)

| Field        | Value                           |
|--------------|---------------------------------|
| systemd unit | `alpaca_orb_bite_bot.service`   |
| Logs         | journald only                   |

**Authoritative commands:**

```bash
systemctl status alpaca_orb_bite_bot.service
journalctl -u alpaca_orb_bite_bot.service -f
systemctl cat alpaca_orb_bite_bot.service
systemctl restart alpaca_orb_bite_bot.service   # ⚠️ confirm before running
systemctl stop alpaca_orb_bite_bot.service      # ⚠️ destructive — confirm first
```

See `link/context/services/alpaca-orb-bite-bot.md` for full detail.

---

### 3.3 LinkedIn Data Centre News Bot

**Execution model:** Docker container, cron-style scheduling via `supercronic` inside the container (not directly systemd-managed)

| Field          | Value                                              |
|----------------|----------------------------------------------------|
| Container name | `linkedin_data_centre_news-linkedin-cron-1`        |
| Scheduler      | `supercronic` (inside container)                   |
| Logs           | Docker container logs                              |

**Authoritative commands:**

```bash
docker ps
docker logs -f linkedin_data_centre_news-linkedin-cron-1
docker exec -it linkedin_data_centre_news-linkedin-cron-1 sh
docker restart linkedin_data_centre_news-linkedin-cron-1   # ⚠️ confirm before running
docker stop linkedin_data_centre_news-linkedin-cron-1      # ⚠️ destructive — confirm first
```

> systemd only ensures Docker itself is up; it does not manage this container directly.  
> Scheduling and logs are handled inside the container.

See `link/context/services/linkedin-data-centre-news-bot.md` for full detail.

---

## 4. Fast Triage Checklist

1. **Identify the bot name.**
2. **Decide: systemd or Docker?** (check the table in §2 above)
3. **Use exactly one of:**
   - `journalctl -u <unit>` → for systemd bots (crypto, alpaca)
   - `docker logs -f <container>` → for Docker bots (LinkedIn)
4. **Never mix tools** — do not run `docker ps` for a systemd bot or `journalctl` for a Docker-only container.

---

*Last updated: 2026-05-03*
