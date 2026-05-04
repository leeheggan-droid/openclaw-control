# Host Overview — srv1501082

**Provider:** Hostinger KVM 2
**IP:** 72.61.123.4
**Hostname:** srv1501082.hstgr.cloud
**SSH user:** jacks
**OS:** Ubuntu (systemd)

## Running Services

### systemd
| Unit | Status | Notes |
|---|---|---|
| openclaw-agent.service | Running | GitHub Issue Polling Agent — /opt/openclaw-agent/agent.py |
| openclaw-crypto.service | Running | Kraken crypto bot — REAL GBP |
| openclaw-vibe-gateway.service | Exited (bridge) | Vibe gateway |
| alpaca_orb_bite_bot.service | Running | Alpaca paper trading bot |
| linkedin-news.timer | Waiting | Fires Sun 22:00 UTC weekly |

## What is NOT here
- No Telegram bot
- No SSH-to-Claude proxy
- No link container
- No openclaw Docker web app (removed 2026-05-04 — replaced by Vercel/Link app)

## Domain
- leeheggan.tech → Vercel (Link app)
- www.leeheggan.tech → Vercel (Link app)
