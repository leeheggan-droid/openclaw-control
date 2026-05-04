# Host Overview — srv1501082

**Provider:** Hostinger KVM 2
**IP:** 72.61.123.4
**Hostname:** srv1501082.hstgr.cloud
**SSH user:** jacks
**OS:** Ubuntu (systemd)

## Running Services

### Docker (compose) — /docker/openclaw-1ne6/
| Container | Status | Notes |
|---|---|---|
| openclaw-1ne6-openclaw-1 | Running | Openclaw app, port 43248 |
| openclaw-1ne6-openclaw-cron-1 | Running | supercronic, shares /data volume |
| traefik | Running | SSL + routing for www.leeheggan.tech |

### systemd
| Unit | Status | Notes |
|---|---|---|
| openclaw-crypto.service | Running | Kraken crypto bot — REAL GBP |
| alpaca_orb_bite_bot.service | Running | Alpaca paper trading bot |
| linkedin-news.timer | Waiting | Fires Sun 22:00 UTC weekly |

## What is NOT here
- No Telegram bot
- No SSH-to-Claude proxy
- No link container
- No /etc/openclaw/ config directory

## Domain
- leeheggan.tech → Vercel (static site)
- www.leeheggan.tech → Traefik → openclaw container (port 43248)
