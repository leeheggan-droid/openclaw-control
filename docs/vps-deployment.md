# OpenClaw Cockpit — VPS Deployment Guide

This guide covers moving the OpenClaw cockpit web app from a local machine to a
VPS and serving it securely via Caddy.

---

## 1. DNS setup

### Current state

The following records have already been configured (TTL ≈ 50 s):

| Name    | Type  | Value             | Status |
|---------|-------|-------------------|--------|
| `@`     | A     | `72.61.123.4`     | ✅ done |
| `www`   | CNAME | `leeheggan.tech.` | ✅ done |

> Replace `72.61.123.4` with your actual VPS IP if it ever changes.

This means the cockpit is reachable at **https://leeheggan.tech** and
**https://www.leeheggan.tech** once Caddy is running.

### Optional: cockpit subdomain

To also serve the cockpit at `https://cockpit.leeheggan.tech`, add one more
record in your DNS provider:

| Name      | Type | Value         |
|-----------|------|---------------|
| `cockpit` | A    | `72.61.123.4` |

The `Caddyfile` in this repo already includes the `cockpit.leeheggan.tech`
block; it becomes active as soon as the DNS record exists and propagates.

> DNS propagation can take up to 24 hours, but with TTL 50 s it is usually
> under a minute.


## 2. Environment file

All runtime secrets and settings live at `/etc/openclaw-control.env` on the VPS.
Create or update this file (root-readable only):

```bash
sudo nano /etc/openclaw-control.env
sudo chmod 600 /etc/openclaw-control.env
```

Minimum required variables:

```ini
OPENAI_API_KEY=sk-...

# SSH lanes
OPENCLAW_SSH_HOST=user@<vps-or-target-host>
OPENCLAW_SSH_READONLY_HOST=readonly@<vps-or-target-host>

# Repo path on the SSH target
OPENCLAW_REPO_DIR=/path/to/repo

# Optional — GitHub integration
GITHUB_TOKEN=ghp_...         # ⚠️  never commit a real token to source control
GITHUB_REPO=leeheggan-droid/openclaw-control
```

The READONLY lane (`OPENCLAW_SSH_READONLY_HOST`) is used for read-only probes,
snapshots, and the `/ops/ssh-readonly-run` endpoint. The cockpit surfaces it in
the `/config` response so you can verify it is loaded correctly.

---

## 3. SSH keys

The cockpit container needs SSH access to the configured targets
(`OPENCLAW_SSH_HOST` and `OPENCLAW_SSH_READONLY_HOST`).

**Recommended:** create a dedicated key pair for the cockpit service so it only
has access to the keys it needs, and keep them separate from other root keys:

```bash
sudo mkdir -p /etc/openclaw-ssh
sudo ssh-keygen -t ed25519 -f /etc/openclaw-ssh/id_ed25519 -N "" -C "openclaw-cockpit"
sudo chmod 700 /etc/openclaw-ssh
sudo chmod 600 /etc/openclaw-ssh/id_ed25519

# Authorise the key on each SSH target (run as the SSH user on the target host):
#   cat /etc/openclaw-ssh/id_ed25519.pub >> ~/.ssh/authorized_keys
```

Then update `docker-compose.cockpit.yml` to mount the dedicated directory:

```yaml
volumes:
  - /etc/openclaw-ssh:/root/.ssh:ro
  - cockpit_data:/app/data
```

**Quick-start alternative:** if you want to reuse existing root SSH keys
immediately, mount `/root/.ssh` instead (as the default compose file does).
Be aware this gives the container access to *all* keys under that directory.

Whichever approach you use, ensure the VPS host key(s) are accepted before
starting the container so SSH does not prompt interactively:

```bash
ssh -i /etc/openclaw-ssh/id_ed25519 \
    -o StrictHostKeyChecking=accept-new \
    $OPENCLAW_SSH_HOST true
ssh -i /etc/openclaw-ssh/id_ed25519 \
    -o StrictHostKeyChecking=accept-new \
    $OPENCLAW_SSH_READONLY_HOST true
```

---

## 4. Start the cockpit

Choose **one** of the two options below.  Both end up with the web app listening
on `http://127.0.0.1:8001`, which Caddy then proxies to your domain.

### Option A — systemd (recommended for direct VPS installs)

This approach runs uvicorn directly as a systemd service — no Docker required.

```bash
# 1. Create the system user (skip if it already exists)
sudo useradd -r -s /usr/sbin/nologin openclaw-agent

# 2. Clone the repo (if not already present)
sudo git clone https://github.com/leeheggan-droid/openclaw-control.git \
    /opt/openclaw-control
sudo chown -R openclaw-agent:openclaw-agent /opt/openclaw-control

# 3. Install Python dependencies into the system Python
cd /opt/openclaw-control
sudo pip3 install -r requirements.txt

# 4. Install and start the systemd service
sudo cp systemd/openclaw-cockpit.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-cockpit

# Check it started correctly
sudo systemctl status openclaw-cockpit
journalctl -u openclaw-cockpit -n 30
```

### Option B — Docker Compose (Traefik)

```bash
cd /opt/openclaw-control

# Pull the latest code
git pull

# Build the image and start the container in the background
docker compose -f docker-compose.cockpit.yml up -d --build
```

Verify the container is healthy:

```bash
docker ps --filter name=openclaw-cockpit
docker logs openclaw-cockpit --tail 30
```

The cockpit is now reachable on the VPS at `http://127.0.0.1:8001`.

---

## 5. Install and configure Caddy

### Install Caddy (Debian/Ubuntu)

```bash
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install caddy
```

### Deploy the Caddyfile

```bash
sudo cp /opt/openclaw-control/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
# Or on first start:
sudo systemctl enable --now caddy
```

The `Caddyfile` in this repo serves the cockpit from all active hostnames:

```
leeheggan.tech, www.leeheggan.tech {
    reverse_proxy 127.0.0.1:8001
}

# Active once the cockpit A record is added in DNS.
cockpit.leeheggan.tech {
    reverse_proxy 127.0.0.1:8001
}
```

Caddy will automatically obtain Let's Encrypt TLS certificates for every
hostname that resolves to this server.

### Firewall

Ports **80** and **443** must be open for Caddy/Let's Encrypt. Port **8001**
should be firewalled so it is not directly reachable from the internet:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw deny 8001/tcp   # cockpit is behind Caddy; no direct external access
```

---

## 6. Verify the deployment

```bash
# Health check via root domain (DNS already active)
curl -sf https://leeheggan.tech/health || echo "no /health endpoint"

# Config endpoint — verify SSH lanes are loaded
curl -s https://leeheggan.tech/config | python3 -m json.tool

# READONLY lane check
curl -s -X POST https://leeheggan.tech/ops/ssh-readonly-run \
    -H "Content-Type: application/json" \
    -d '{"cmd":"uptime"}' | python3 -m json.tool

# Once the cockpit A record is added, the same checks work via the subdomain:
#   curl -s https://cockpit.leeheggan.tech/config | python3 -m json.tool
```

The `/config` response should include:

```json
{
  "ssh_host": "...",
  "ssh_readonly_host": "...",
  "repo_dir": "...",
  ...
}
```

---

## 7. Restarting / updating

```bash
cd /opt/openclaw-control
git pull
docker compose -f docker-compose.cockpit.yml up -d --build
```

The `restart: unless-stopped` policy in `docker-compose.cockpit.yml` ensures the
container comes back up automatically after a VPS reboot.

---

## 8. Telegram bot (optional)

The bot relays Telegram messages to the `/chat` endpoint.

### Start as a systemd service

```bash
sudo cp systemd/openclaw-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-telegram-bot
```

### Required environment variable

Make sure `/etc/openclaw-control.env` contains your bot token:

```ini
TELEGRAM_BOT_TOKEN=<token-from-BotFather>
# The bot calls the local web app; keep the default unless the port changes.
CHAT_API_URL=http://127.0.0.1:8001
```

After editing the file, restart the service:

```bash
sudo systemctl restart openclaw-telegram-bot
```

### Viewing errors

The bot logs **warnings and errors** to a rotating file at
`/opt/openclaw-control/telegram_bot_error.log` in addition to journald:

```bash
# Live journal stream
journalctl -u openclaw-telegram-bot -f

# Error log file (warnings + errors only)
tail -f /opt/openclaw-control/telegram_bot_error.log
```

Common causes of startup failure:

| Error message | Fix |
|---------------|-----|
| `TELEGRAM_BOT_TOKEN is not set` | Add the token to `/etc/openclaw-control.env` and restart |
| `Unauthorized` / `401` | The token is invalid — regenerate it via @BotFather |
| `Failed to build Telegram application` | Check the full stack trace in `telegram_bot_error.log` |
| Bot starts but chat replies fail | Verify the cockpit is running (`systemctl status openclaw-cockpit`) |

---

## 9. Troubleshooting

| Symptom | Action |
|---------|--------|
| Cockpit not reachable at domain | Check `systemctl status openclaw-cockpit` and `systemctl status caddy` |
| Container restart loop | `docker logs openclaw-cockpit --tail 50` |
| `ModuleNotFoundError: No module named 'agents'` | Rebuild the image — `openai-agents` is now in `requirements.txt` |
| `ImportError: cannot import name 'Agent' from 'agents' (…/openclaw_control/agents/__init__.py)` | Do **not** set `PYTHONPATH=/app/openclaw_control`; the local `agents/` sub-package shadows the pip package |
| Certificate not issued | Check DNS propagation (`dig leeheggan.tech`), verify ports 80/443 open |
| SSH connection refused from container | Verify `/root/.ssh` is mounted and `known_hosts` contains the target |
