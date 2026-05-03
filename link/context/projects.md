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

## openclaw-crypto (Crypto Bot)
**Status:** Active  
**Service name:** `openclaw-crypto`  
**Repo:** leeheggan-droid/openclaw-crypto (separate repo — this entry covers control integration only)  
**Purpose:** Automated crypto trading bot running as a Docker Compose service on the VPS

### Service context
See `link/context/services/openclaw-crypto.md` for the full interaction guide.

### Current State
- Managed via `openclaw-control` Ansible playbook with `-e "service=openclaw-crypto"`
- Supported operations: `status`, `up`, `down`, `restart`, `deploy`, `logs`

### Next Steps
- [ ] Confirm `service` variable scoping works in all Ansible task files (`up.yml`, `down.yml`, etc.)
- [ ] Verify the crypto bot image/container name matches `openclaw-crypto` in `docker-compose.yml`
- [ ] Document any API keys or environment variables the service requires (without committing secrets)
