# Key Decisions

## 2025-01-XX — Context System Structure
**Decision:** Use markdown files in `link/context/` for persistent memory  
**Rationale:** 
- Human-readable and version-controlled
- Easy to update via GitHub API
- Can be loaded into context at session start
- Grep-friendly for retrieval

**Alternatives considered:**
- JSON/YAML structured data — less readable, harder to scan
- Database — overkill for current scale, adds infrastructure complexity

---

## 2025-01-XX — openclaw-control Architecture
**Decision:** Ansible playbooks triggered via GitHub Actions workflow_dispatch  
**Rationale:**
- No direct SSH exposure needed from Link's runtime
- Audit trail via GitHub Actions logs
- Can trigger from any environment with GitHub API access
- Ansible handles idempotency and complex orchestration

**Alternatives considered:**
- Direct SSH from Link — security concerns, credential management complexity
- Dedicated CI/CD platform — unnecessary overhead

---

## 2025-01-XX — Destructive Action Confirmation
**Decision:** Always require explicit user confirmation for: down, deploy, merges to main, credential changes, order placement  
**Rationale:**
- Money and uptime are at stake
- Easy to say "yes" when intended, impossible to undo when not
- Builds trust in the system

---

## 2026-05-04 — Remove Old Openclaw Web App Docker Stack
**Decision:** Remove all Docker Compose infrastructure for the old Openclaw web app (containers `openclaw-1ne6-openclaw-1`, `openclaw-1ne6-openclaw-cron-1`, Traefik) and its Ansible task files  
**Rationale:**
- The old Openclaw Wizard AI web app is abandoned technology
- `www.leeheggan.tech` is now served from Vercel via the Link repo — Traefik is no longer needed
- Removing the Docker stack and its 6 Ansible task files keeps the control repo focused on what it actually manages: systemd bot services
- Ansible actions `status`, `up`, `down`, `restart`, `deploy`, `logs` all targeted `/docker/openclaw-1ne6/` and have been removed

**Alternatives considered:**
- Keep Docker task files as dead code — rejected (misleading, creates confusion)
- Keep Traefik as standalone container — rejected (serves no purpose without the openclaw app)

**Revisit if:** A new Docker service is added to the VPS that needs Compose management

---



## YYYY-MM-DD — [Decision Title]
**Decision:** [What was decided]  
**Rationale:** [Why this choice]  
**Alternatives considered:** [What else was on the table]  
**Revisit if:** [Conditions that would change this]
