# openclaw-control — Server Context

**Server:** srv1501082.hstgr.cloud / 72.61.123.4
**SSH user:** jacks
**Updated:** 2026-05-04

## Docker Stack — /docker/openclaw-1ne6/

| Container | Role | Port |
|---|---|---|
| openclaw-1ne6-openclaw-1 | Openclaw app (Wizard AI agent) | 43248 |
| openclaw-1ne6-openclaw-cron-1 | supercronic cron runner | — |
| traefik | Reverse proxy, SSL | 80/443 |

- Domain: www.leeheggan.tech → Traefik → openclaw container
- Volumes: ./data:/data, /opt/openclaw/config, /opt/openclaw/skills, /opt/openclaw/cache, /opt/openclaw/logs
- Env: /docker/openclaw-1ne6/.env

## systemd Services

| Service | Description | Location |
|---|---|---|
| openclaw-crypto.service | Kraken crypto trading bot (REAL GBP) | /home/jacks/openclaw-crypto/ |
| alpaca_orb_bite_bot.service | Alpaca stock bot (paper trading) | /home/jacks/alpaca_orb_bite_bot/ |
| linkedin-news.timer | LinkedIn DC news bot — fires Sun 22:00 UTC | /home/jacks/LinkedIn_Data_Centre_News/ |

## Key Paths

| Path | Purpose |
|---|---|
| /docker/openclaw-1ne6/ | Docker compose project |
| /home/jacks/openclaw-crypto/ | Crypto bot source + secrets |
| /home/jacks/alpaca_orb_bite_bot/ | Alpaca bot source + venv |
| /home/jacks/LinkedIn_Data_Centre_News/ | LinkedIn bot source |
| /opt/openclaw/ | Openclaw config, skills, cache, logs (mounted into container) |

## Notes
- leeheggan.tech is hosted on Vercel — Traefik only handles www.leeheggan.tech → openclaw app
- Crypto bot uses Kraken API (real money — do not restart without checking open positions)
- Alpaca bot is paper trading only
- LinkedIn bot runs weekly via systemd timer (not Docker cron)
