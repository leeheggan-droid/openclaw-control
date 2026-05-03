# Active Projects

## openclaw-control
**Status:** Active — foundation laid  
**Repo:** leeheggan-droid/openclaw-control  
**Purpose:** Ansible-based control layer for managing Dockerised bots on Ubuntu VPS

### Current State
- GitHub Actions workflow (`link.yml`) triggers Ansible playbooks via workflow_dispatch
- Available tasks: up, down, restart, status, deploy, logs
- Link can trigger operations and check results via `openclaw_run_task` and `openclaw_get_status`

### Next Steps
- [ ] Verify VPS connectivity and SSH key setup
- [ ] Test full deploy cycle
- [ ] Add monitoring/alerting for container health

---

## Link (This System)
**Status:** Active — bootstrapping  
**Purpose:** Operational AI layer with persistent memory, tool access, and action capability

### Current State
- Core identity and operational principles defined
- Tool integrations: GitHub, Kraken, Alpaca, Telegram, Brave Search
- Context system being established (this file)

### Next Steps
- [ ] Populate context files with current system knowledge
- [ ] Establish memory refresh patterns
- [ ] Build out voice interface prototype

---

## Bot Services (Host Overview)

> See `link/context/services/host-overview.md` for the master operations brief.
> **Always identify the execution layer before running any command** — some bots
> are systemd-native, others are Docker containers.

### openclaw-crypto (Crypto Bot)
**Status:** Active  
**Execution model:** systemd (`openclaw-crypto.service`) — **NOT Docker**  
**Repo:** leeheggan-droid/openclaw-crypto  
**Service context:** `link/context/services/openclaw-crypto.md`

### Alpaca ORB Bite Bot
**Status:** Active  
**Execution model:** systemd (`alpaca_orb_bite_bot.service`) — **NOT Docker**  
**Service context:** `link/context/services/alpaca-orb-bite-bot.md`

### LinkedIn Data Centre News Bot
**Status:** Active  
**Execution model:** Docker container (`linkedin_data_centre_news-linkedin-cron-1`)  
**Scheduler:** supercronic (inside container)  
**Service context:** `link/context/services/linkedin-data-centre-news-bot.md`
