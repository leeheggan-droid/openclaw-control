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
#   Required: OPENAI_API_KEY, AUTH_SECRET_KEY, AUTH_ADMIN_DEFAULT_PASSWORD

# 5. Initialise the database and create the admin account (first time only)
export AUTH_ADMIN_EMAIL=leeheggan@gmail.com
export AUTH_ADMIN_DEFAULT_PASSWORD=YourStrongPassword123!
python init_db.py

# 6. Run the web app
uvicorn web_app:app --reload --port 8001

# Open http://localhost:8001/login in your browser
# Log in with: leeheggan@gmail.com / <your AUTH_ADMIN_DEFAULT_PASSWORD>
```

> **Security:** Set `AUTH_SECRET_KEY` to a long random string and
> `AUTH_ADMIN_DEFAULT_PASSWORD` to a strong, unique password in your `.env`
> **before the first boot**. The placeholder values in `config.env.example`
> must never be used in production.

---

## First-time initialisation

Run `init_db.py` **before** starting the web app on any fresh install or new
server.  It is safe to re-run — it will not overwrite an existing admin
account.

```bash
# Optional: override defaults via environment variables
export CHAT_DB_PATH=data/chat.db            # default: data/chat.db
export AUTH_ADMIN_EMAIL=leeheggan@gmail.com # default: leeheggan@gmail.com
export AUTH_ADMIN_DEFAULT_PASSWORD=secret   # default: changeme123 (change this!)

python init_db.py
```

The script will:
1. Verify `bcrypt` is installed and working.
2. Create the `data/` directory if it does not exist.
3. Create the `auth_users` table in the SQLite database.
4. Insert the admin user (if not already present).
5. Print the absolute database path and confirm success.

### Verify the admin was created

```bash
sqlite3 data/chat.db "SELECT email, created_at FROM auth_users;"
```

Expected output:
```
leeheggan@gmail.com|2026-01-01T00:00:00.000Z
```

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ModuleNotFoundError: No module named 'bcrypt'` | `pip install bcrypt` |
| `PermissionError` on `data/` | `sudo chown -R $USER data/` — or check that your user can write to the project directory |
| bcrypt error on `_ensure_admin()` | Reinstall: `pip install --upgrade bcrypt` |
| Admin not appearing in DB after `init_db.py` | Re-run with `AUTH_ADMIN_DEFAULT_PASSWORD` set; check the script output for errors |
| Web app starts but login fails | Confirm `AUTH_ADMIN_EMAIL` and `AUTH_ADMIN_DEFAULT_PASSWORD` match what was set when `init_db.py` was run |

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
| `AUTH_ADMIN_DEFAULT_PASSWORD` | *(required — no default in production)* | Password for admin account on first boot — set to a strong secret |
| `AUTH_TOKEN_EXPIRE_HOURS` | `168` (7 days) | JWT session lifetime |

### VPS / SSH integration

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENCLAW_SSH_HOST` | *(empty — mutative SSH disabled)* | SSH alias/user@host for write operations (git pull, docker restart, vibe runs) |
| `OPENCLAW_SSH_READONLY_HOST` | *(empty — read-only SSH disabled)* | SSH alias/user@host for read-only probes, autopilot evidence, terminal pills. **Separate user from `OPENCLAW_SSH_HOST`** |
| `OPENCLAW_REPO_DIR` | *(empty)* | Absolute path of the trading repo on the VPS (e.g. `/opt/openclaw-crypto`) |
| `OPENCLAW_VIBE_WORKDIR` | falls back to `OPENCLAW_REPO_DIR` | Default working directory for Vibe plan/execute |

### Cheap Chat / alternative LLM providers

| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | *(empty)* | Groq API key (enables Groq in Cheap Chat tab) |
| `MISTRAL_API_KEY` | *(empty)* | Mistral API key (enables Mistral in Cheap Chat tab) |
| `CEREBRAS_API_KEY` | *(empty)* | Cerebras API key (enables Cerebras in Cheap Chat tab) |
| `BRAVE_API_KEY` | *(empty)* | Brave Search API key (enables `web_search` agent tool) |

### Exchange APIs (read-only trade history)

| Variable | Default | Description |
|----------|---------|-------------|
| `KRAKEN_API_KEY` | *(empty)* | Kraken API key (read-only; used by `get_trade_history` tool) |
| `KRAKEN_API_SECRET` | *(empty)* | Kraken API secret |
| `ALPACA_API_KEY` | *(empty)* | Alpaca API key (read-only filled-orders; also accepts `APCA_API_KEY_ID`) |
| `ALPACA_API_SECRET` | *(empty)* | Alpaca API secret (also accepts `APCA_API_SECRET_KEY`) |

### Trade inactivity alerting

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENCLAW_TRADE_INACTIVITY_HOURS` | `12` | Hours without a logged trade before an alert fires |
| `OPENCLAW_ALERT_WEBHOOK_URL` | *(empty)* | Webhook URL for inactivity alerts (e.g. Discord, Slack) |

### Telegram bot

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | *(required to run bot)* | Token from @BotFather |
| `CHAT_API_URL` | `http://127.0.0.1:8001` | Base URL of the web app |

---

## AI agent integration

The main AI agent ("Main AI") is fully integrated with the cockpit backend.  All
agent interactions happen through `POST /agent/message` (body: `{agent, text,
workspace}`).  The agent has the following tools available:

| Tool | What it does | Requires |
|------|-------------|---------|
| `run_vibe_report(report_id)` | SSH read-only probes (containers, trades, P&L, git HEAD) | `OPENCLAW_SSH_READONLY_HOST` |
| `get_trade_history(limit)` | Local SQLite log → Kraken API → Alpaca API, auto-fallback | Optionally `KRAKEN_API_KEY`, `ALPACA_API_KEY` |
| `get_pnl_history(limit)` | Local SQLite P&L snapshot log | *(none — local DB)* |
| `ask_pnl(text)` | Delegate to the P&L specialist agent | `OPENAI_API_KEY` |
| `ask_quant(text)` | Delegate to the Quant specialist agent | `OPENAI_API_KEY` |
| `web_search(query)` | Brave web search | `BRAVE_API_KEY` |
| `list_registered_users()` | List users in the local auth database | *(none — local DB)* |

### Enabling VPS / SSH integration

1. **Create a read-only SSH user on your VPS** (recommended):
   ```bash
   sudo adduser --disabled-password openclaw-readonly
   sudo mkdir -p /home/openclaw-readonly/.ssh
   sudo cp ~/.ssh/authorized_keys /home/openclaw-readonly/.ssh/
   sudo chown -R openclaw-readonly: /home/openclaw-readonly/.ssh
   ```
   See [`docs/ssh-execution-gateway.md`](docs/ssh-execution-gateway.md) for the
   full `bin/vibe-readonly-wrapper.sh` forced-command setup.

2. **Set the environment variables**:
   ```bash
   # In .env or /etc/openclaw-control.env
   OPENCLAW_SSH_HOST=user@your-vps-ip
   OPENCLAW_SSH_READONLY_HOST=openclaw-readonly@your-vps-ip
   OPENCLAW_REPO_DIR=/opt/openclaw-crypto
   ```

3. **Verify** by calling the read-only SSH health endpoint:
   ```bash
   curl -s http://localhost:8001/ops/report?report_id=container_health | jq .
   ```

### Database query capabilities

Once the app is running, the agent and the REST API can query the local SQLite
database.

**Via the agent** — ask the Main AI directly:
> "List all registered users"
> "How many users are in the system?"

The `list_registered_users()` tool returns email addresses and registration
dates.  Password hashes are never returned.

**Via REST** (requires authentication cookie):
```bash
# 1. Log in to get a session cookie
curl -c /tmp/oc.cookies -s -X POST http://localhost:8001/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"leeheggan@gmail.com","password":"<your_password>"}' | jq .

# 2. Query the user list
curl -b /tmp/oc.cookies -s http://localhost:8001/admin/users | jq .
```

Expected response:
```json
{
  "users": [
    {"email": "admin@yourdomain.com", "created_at": "2026-04-29T07:33:00.000Z"}
  ]
}
```

### Vibe reports (pre-defined read-only SSH probes)

| `report_id` | What it fetches |
|-------------|----------------|
| `container_health` | `docker ps` + restart counts |
| `last_trade` | Last 10 trades (local DB, then VPS SSH fallback) |
| `trade_history_7d` | Last 200 trades |
| `pnl_snapshot` | P&L, equity, drawdown, Sharpe |
| `halt_status` | HALT / risk / paused state |
| `per_trade_analytics` | Gross/net P&L, fees, slippage, reason codes |
| `git_head` | Current git HEAD SHA on the VPS |

Run a report directly:
```bash
curl -s -X POST "http://localhost:8001/ops/report?report_id=container_health" | jq .output
```

### Access control summary

| Endpoint | Auth required |
|----------|-------------|
| `POST /agent/message` | No — intended to be behind your reverse proxy/VPN |
| `GET /admin/users` | **Yes** — valid session cookie (`openclaw_session`) |
| `POST /chat` | Yes — session cookie or explicit `user_id` |
| `GET /chat-web` | Yes — redirects to `/login` |
| `POST /ops/report` | No — read-only; safe behind your firewall |
| `POST /ops/ssh-readonly-run` | No — read-only; safe behind your firewall |

> **Security note:** The agent/message and ops endpoints have no auth guard
> by design — the cockpit is intended to run on a private VPS and be exposed
> only through an authenticated reverse proxy (Caddy/nginx with HTTP basic auth,
> Tailscale, or IP allowlist).  Do **not** expose port 8001 directly to the
> internet.

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

## Contributing / keeping your environment clean

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for:

- How to use `git status`, `git stash`, and `git clean` before pulling
- Recreating the `.venv` Python environment after a clean or fresh clone
- Preserving local `.env` secrets and Docker overrides between pulls
- Fixing file-permission errors on shared VPS hosts

---

## Safety checklist

- [ ] No secrets or credentials added to source code
- [ ] No destructive operations introduced
- [ ] Changes limited to the minimum required by the issue
- [ ] Local test passed: `uvicorn web_app:app --reload` then verified in browser
