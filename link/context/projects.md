# Active Projects

## openclaw-control
**Status:** Active  
**Repo:** leeheggan-droid/openclaw-control  
**Purpose:** Ansible-based control layer for managing systemd bot services on Ubuntu VPS via GitHub Actions

### Current State
- GitHub Actions workflow (`link.yml`) triggers Ansible playbooks via workflow_dispatch
- Available actions: `status-all`, `systemd-status`, `systemd-logs`, `systemd-restart`, `logs-systemd`
- Link triggers operations via the GitHub API (`workflow_dispatch` POST to `link.yml`)
- Results visible in GitHub Actions run logs

### Next Steps
- [ ] Verify VPS connectivity and SSH key setup
- [ ] Test full restart cycle via GitHub Actions
- [ ] Add monitoring/alerting for service health

---

## Link (This System)
**Status:** Active  
**Repo:** leeheggan-droid/link (deployed on Vercel at www.leeheggan.tech)  
**Purpose:** Operational AI layer with persistent memory, tool access, and action capability

### Current State
- Core identity and operational principles defined
- Tool integrations: GitHub, Kraken, Alpaca, Telegram, Brave Search
- Context system established in `link/context/`
- Interacts with this repo to trigger VPS operations (see `link/context/how-link-interacts.md`)
- **Intelligent model router active** — routes turns to Groq / Gemini Flash / Claude based on task type; see `link/context/model-router.md`

### Next Steps
- [ ] Establish memory refresh patterns
- [ ] Build out voice interface prototype

---

## Bot Services (Host Overview)

> See `link/context/services/host-overview.md` for the master operations brief.
> **Always identify the execution layer before running any command** — all bots
> are systemd-native on the VPS.

### openclaw-agent (GitHub Issue Polling Agent)
**Status:** Active  
**Execution model:** systemd (`openclaw-agent.service`) — NOT Docker  
**Location:** /opt/openclaw-agent/  
**Service context:** `link/context/services/openclaw-agent.md`

### openclaw-crypto (Crypto Bot)
**Status:** Active  
**Execution model:** systemd (`openclaw-crypto.service`) — NOT Docker  
**Repo:** leeheggan-droid/openclaw-crypto  
**Service context:** `link/context/services/openclaw-crypto.md`

### Alpaca ORB Bite Bot
**Status:** Active  
**Execution model:** systemd (`alpaca_orb_bite_bot.service`) — NOT Docker  
**Service context:** `link/context/services/alpaca-orb-bite-bot.md`

### LinkedIn Data Centre News Bot
**Status:** Active  
**Execution model:** systemd timer (`linkedin-news.timer` + `linkedin-news.service`)  
**Scheduler:** systemd timer, fires Sun 22:00 UTC  
**Service context:** `link/context/services/linkedin-data-centre-news-bot.md`
