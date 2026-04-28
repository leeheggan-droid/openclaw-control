# OpenClaw Control

OpenClaw is a full-stack AI control cockpit combining a FastAPI web backend,
an authenticated OpenAI chat interface, a Telegram bot, and a suite of
autonomous trading agents — all deployable to a single VPS behind
[www.leeheggan.tech](https://www.leeheggan.tech).

---

## Features

| Feature | Description |
|---------|-------------|
| **OpenAI chat** | `/chat` endpoint with per-user SQLite conversation memory |
| **Web chat UI** | Login-protected chat page at `/chat-web` |
| **Authentication** | Email + password login, bcrypt hashing, JWT session cookies |
| **Telegram bot** | Relay Telegram messages to the same `/chat` backend |
| **Trading cockpit** | Existing autopilot, P&L, and agent dashboards |
| **VPS deployment** | Docker Compose + Traefik (TLS) or nginx + Let's Encrypt |

---

## Quick start (local development)

```bash
# 1. Clone
git clone https://github.com/leeheggan-droid/openclaw-control.git
cd openclaw-control

# 2. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and fill in environment variables
cp config.env.example .env
#   At minimum set: OPENAI_API_KEY, AUTH_SECRET_KEY

# 5. Run the web app
uvicorn web_app:app --reload --port 8001

# Open http://localhost:8001/login in your browser
# Default credentials: leeheggan@gmail.com / changeme123
```

> **Important:** Change the default password immediately via the "Change password"
> button in the chat UI, or set `AUTH_ADMIN_DEFAULT_PASSWORD` in your `.env` before
> the first boot.

---

## Environment variables

### Required

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key used by the chat endpoint and Agents SDK |
| `AUTH_SECRET_KEY` | Random secret for signing JWT cookies (≥ 32 chars) |

### Chat & auth

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_CHAT_MODEL` | `gpt-4o-mini` | OpenAI model for `/chat` |
| `CHAT_SYSTEM_PROMPT` | *(built-in)* | Override the LLM system prompt |
| `CHAT_DB_PATH` | `data/chat.db` | Path to the SQLite database |
| `CHAT_MAX_HISTORY` | `40` | Maximum past messages per OpenAI request |
| `AUTH_ADMIN_EMAIL` | `leeheggan@gmail.com` | Admin account created on first boot |
| `AUTH_ADMIN_DEFAULT_PASSWORD` | `changeme123` | Temporary password — change it! |
| `AUTH_TOKEN_EXPIRE_HOURS` | `168` (7 days) | JWT session lifetime |

### Telegram bot

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | *(required to run bot)* | Token from @BotFather |
| `CHAT_API_URL` | `http://127.0.0.1:8001` | Base URL of the web app |

---

## Web chat endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/login` | `GET` | Login page |
| `/chat-web` | `GET` | Authenticated chat UI (redirects to `/login` if not signed in) |
| `/auth/login` | `POST` | Validate credentials → set session cookie |
| `/auth/logout` | `POST` | Clear session cookie |
| `/auth/change-password` | `POST` | Change password (requires active session) |
| `/chat` | `POST` | Send a message, receive an OpenAI reply |
| `/chat/clear` | `POST` | Clear conversation history |

### `/chat` request body

```json
{
  "message": "Hello, what can you help me with?",
  "user_id": ""
}
```

`user_id` is optional when calling from a browser (the session cookie identifies
the user).  The Telegram bot passes `"tg:<telegram_user_id>"` so each Telegram
user has their own memory thread.

---

## Telegram bot

```bash
# Set your bot token in .env then run:
python -m telegram_bot.bot
```

Commands:
- `/start` or `/help` — show welcome message
- `/clear` — wipe conversation history

The bot's memory is shared with the web chat (same SQLite database, same
`user_id` namespace).

---

## Deployment to www.leeheggan.tech

See [`docs/vps-deployment.md`](docs/vps-deployment.md) for the full Caddy-based
deployment guide (TLS certificates issued automatically by Let's Encrypt).

### Quick path — Docker Compose + Traefik

```bash
# On the VPS
sudo cp config.env.example /etc/openclaw-control.env
sudo nano /etc/openclaw-control.env          # fill in real secrets
sudo chmod 600 /etc/openclaw-control.env

cd /opt/openclaw-control
git pull
docker compose -f docker-compose.cockpit.yml up -d --build
```

Traefik will obtain a Let's Encrypt certificate for `leeheggan.tech` and
`www.leeheggan.tech` automatically as long as ports 80 and 443 are open and
the DNS A record points to your VPS.

### Alternative — nginx + systemd

1. Deploy the web app as a systemd service (see `systemd/openclaw-agent.service`
   as a template — duplicate it and change `ExecStart` to `uvicorn web_app:app …`).
2. Copy `nginx/openclaw.conf` to `/etc/nginx/sites-available/openclaw`.
3. Issue a Let's Encrypt cert:
   ```bash
   sudo certbot --nginx -d leeheggan.tech -d www.leeheggan.tech
   ```
4. Enable and reload nginx.

### Telegram bot as a systemd service

```bash
sudo cp systemd/openclaw-telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-telegram-bot
```

---

## DNS setup

Your DNS is already configured (see [`docs/vps-deployment.md`](docs/vps-deployment.md)):

| Name | Type  | Value           |
|------|-------|-----------------|
| `@`  | A     | `72.61.123.4`   |
| `www`| CNAME | `leeheggan.tech` |

If your VPS IP ever changes, update the A record in your DNS provider (Tucows
Registrar / current nameservers: `byte.dns-parking.com`, `pixel.dns-parking.com`).

---

## Adding new LLM providers

1. Edit `chat_feature.py` — change `_MODEL` or add a provider branch.
2. For a completely different provider (e.g. Anthropic/Claude), swap
   `openai.OpenAI(...)` for the relevant SDK client and update
   `requirements.txt`.

---

## Safety checklist

- [ ] No secrets or credentials added to source code
- [ ] No destructive operations introduced
- [ ] Changes limited to the minimum required by the issue
- [ ] Local test passed: `uvicorn web_app:app --reload` then verified in browser
