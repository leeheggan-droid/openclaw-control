# openclaw-control — Server Context

**Server:** srv1501082.hstgr.cloud / 72.61.123.4
**SSH user:** jacks
**Updated:** 2026-05-04

## systemd Services

| Service | Description | Location |
|---|---|---|
| openclaw-agent.service | GitHub Issue Polling Agent | /opt/openclaw-agent/ |
| openclaw-crypto.service | Kraken crypto trading bot (REAL GBP) | /home/jacks/openclaw-crypto/ |
| openclaw-vibe-gateway.service | Vibe gateway | (wraps Docker) |
| alpaca_orb_bite_bot.service | Alpaca stock bot (paper trading) | /home/jacks/alpaca_orb_bite_bot/ |
| linkedin-news.timer | LinkedIn DC news bot — fires Sun 22:00 UTC | /home/jacks/LinkedIn_Data_Centre_News/ |

## Key Paths

| Path | Purpose |
|---|---|
| /opt/openclaw-agent/ | GitHub Issue Polling Agent source + venv |
| /home/jacks/openclaw-crypto/ | Crypto bot source + secrets |
| /home/jacks/alpaca_orb_bite_bot/ | Alpaca bot source + venv |
| /home/jacks/LinkedIn_Data_Centre_News/ | LinkedIn bot source |

## Notes
- leeheggan.tech and www.leeheggan.tech are both hosted on Vercel (the Link app repo)
- Crypto bot uses Kraken API (real money — do not restart without checking open positions)
- Alpaca bot is paper trading only
- LinkedIn bot runs weekly via systemd timer
