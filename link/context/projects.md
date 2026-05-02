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

## Trading Bots (Planned)
**Status:** Not started  
**Purpose:** Automated trading strategies running as Docker containers

### Notes
- Will be managed via openclaw-control
- Kraken and Alpaca integrations available for market data and execution
- Strategy development pending
