# Service: LinkedIn Data Centre News Bot

> Persistent memory for the `linkedin_data_centre_news` Docker container.
> For the full host context (all bots), see `link/context/services/host-overview.md`.

---

## Service Identity

| Field             | Value                                               |
|-------------------|-----------------------------------------------------|
| Container name    | `linkedin_data_centre_news-linkedin-cron-1`         |
| Execution model   | Docker container (NOT systemd-managed directly)     |
| Scheduler         | `supercronic` (runs inside the container)           |
| VPS               | `srv1501082` / `72.61.123.4`                        |
| VPS user          | `jacks`                                             |
| Logs              | Docker container logs                               |

---

## Authoritative Commands

### Check container is running (safe — run freely)

```bash
docker ps
```

### Stream live logs (safe — run freely)

```bash
docker logs -f linkedin_data_centre_news-linkedin-cron-1
```

### Open a shell inside the container (safe — read-only intent)

```bash
docker exec -it linkedin_data_centre_news-linkedin-cron-1 sh
```

### Restart container (confirm intent before running)

```bash
docker restart linkedin_data_centre_news-linkedin-cron-1
```

### Stop container ⚠️ destructive — require explicit user confirmation

```bash
docker stop linkedin_data_centre_news-linkedin-cron-1
```

---

## Notes

- systemd only ensures the Docker daemon is running — it does **not** manage
  this container directly.
- Scheduling (cron jobs) and all runtime logs live inside the container;
  `journalctl` will **not** show application logs for this bot.
- If uniformity with systemd-managed bots is needed in future, this container
  could be wrapped with a systemd service unit.

---

*Last updated: 2026-05-03*
