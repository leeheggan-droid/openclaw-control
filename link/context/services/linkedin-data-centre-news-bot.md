# Service: LinkedIn Data Centre News Bot

> Persistent memory for the `linkedin-news` systemd timer.
> For the full host context (all bots), see `link/context/services/host-overview.md`.

---

## Service Identity

| Field             | Value                                          |
|-------------------|------------------------------------------------|
| systemd unit      | `linkedin-news.timer` + `linkedin-news.service`|
| Execution model   | systemd timer — fires Sun 22:00 UTC            |
| Working directory | `/home/jacks/LinkedIn_Data_Centre_News/`       |
| VPS               | `srv1501082` / `72.61.123.4`                   |
| VPS user          | `jacks`                                        |
| Logs              | journald                                       |

---

## Authoritative Commands

### Check timer status (safe — run freely)

```bash
systemctl status linkedin-news.timer
```

### View recent logs (safe — run freely)

```bash
journalctl -u linkedin-news.service --no-pager -n 50
```

### Inspect the timer/service unit files (safe — run freely)

```bash
systemctl cat linkedin-news.timer
systemctl cat linkedin-news.service
```

### Manually trigger the service (outside of schedule)

```bash
sudo systemctl start linkedin-news.service
```

### Stop/disable timer ⚠️ — require explicit user confirmation

```bash
sudo systemctl stop linkedin-news.timer
```

---

## Notes

- Runs weekly via systemd timer (`linkedin-news.timer`), not on-demand.
- All log output goes to journald; use `journalctl -u linkedin-news.service`.
- The `systemd-logs` action in Link Control includes this service.

---

*Last updated: 2026-05-04*
