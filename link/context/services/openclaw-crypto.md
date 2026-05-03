# Service: openclaw-crypto

> Persistent memory for the `openclaw-crypto` Docker Compose service.
> This file is the source of truth for how **control** and **Link** interact with the crypto bot.

---

## Service Identity

| Field            | Value                  |
|------------------|------------------------|
| Service name     | `openclaw-crypto`      |
| Compose project  | `openclaw`             |
| Compose dir      | `/opt/openclaw`        |
| VPS              | `srv1501082` / `72.61.123.4` |
| VPS user         | `jacks`                |

---

## How control manages this service

All operations go through the standard `openclaw-control` Ansible playbook with the optional `service` variable to scope commands to `openclaw-crypto` only.

### Scope to this service only (recommended)

```bash
ansible-playbook -i ansible/inventory ansible/site.yml \
  -e "action=<action>" -e "service=openclaw-crypto"
```

### Affect the entire stack (all services)

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=<action>"
```

### Available actions

| Action    | Effect on `openclaw-crypto`                         | Destructive? |
|-----------|-----------------------------------------------------|--------------|
| `status`  | `docker compose ps openclaw-crypto`                 | No (default) |
| `up`      | `docker compose up -d openclaw-crypto`              | No |
| `down`    | `docker compose down openclaw-crypto`               | **Yes** |
| `restart` | `docker compose restart openclaw-crypto`            | No |
| `deploy`  | `docker compose pull openclaw-crypto` + `up -d`     | **Yes** |
| `logs`    | `docker compose logs --tail=100 openclaw-crypto`    | No |

> ⚠️ `down` and `deploy` are destructive. Link must always ask for explicit
> confirmation before running either of these against `openclaw-crypto`.

---

## How Link interacts with this service

1. **Check status** — safe, run freely:
   ```bash
   ansible-playbook -i ansible/inventory ansible/site.yml \
     -e "action=status" -e "service=openclaw-crypto"
   ```

2. **Read logs** — safe, run freely:
   ```bash
   ansible-playbook -i ansible/inventory ansible/site.yml \
     -e "action=logs" -e "service=openclaw-crypto"
   ```

3. **Start / restart** — safe, confirm intent before running:
   ```bash
   ansible-playbook -i ansible/inventory ansible/site.yml \
     -e "action=restart" -e "service=openclaw-crypto"
   ```

4. **Deploy (new image)** — destructive, require explicit user confirmation:
   ```bash
   ansible-playbook -i ansible/inventory ansible/site.yml \
     -e "action=deploy" -e "service=openclaw-crypto"
   ```

5. **Stop** — destructive, require explicit user confirmation:
   ```bash
   ansible-playbook -i ansible/inventory ansible/site.yml \
     -e "action=down" -e "service=openclaw-crypto"
   ```

> Before triggering any operation, read `link/context/environment.md` to confirm
> whether to use the `LOCAL_SSH` or `GITHUB_ACTIONS` execution path.

---

## Notes

- Service definition lives in `/opt/openclaw/docker-compose.yml` on the VPS.
- The `service` variable scoping (`-e "service=openclaw-crypto"`) requires the
  relevant Ansible task files (`up.yml`, `down.yml`, etc.) to use
  `{{ service | default('') }}` in their `docker compose` commands. Verify this
  is in place before relying on service-scoped operations.

---

*Last updated: 2026-05-03*
