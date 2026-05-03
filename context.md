# Link Context

> Persistent memory file for Link, the operational AI assistant.
> Read at conversation start. Updated as we go.
> **Before taking any operational action, read `link/context/environment.md`
> to determine the active execution context and correct trigger method.**

---

## System Overview

**openclaw-control** is an Ansible-based control layer for managing Dockerised bots on an Ubuntu VPS (`srv1501082` / `72.61.123.4`). Operations are triggered via GitHub Actions `workflow_dispatch`, which runs the corresponding Ansible playbook.

### Available Tasks
| Task | Description | Destructive? |
|------|-------------|--------------|
| `up` | Start containers | No |
| `down` | Stop containers | **Yes** |
| `restart` | Restart containers | No |
| `status` | Check container health | No |
| `deploy` | Pull latest images + recreate | **Yes** |
| `logs` | Fetch recent container logs | No |

### Key Paths
- **Workflow file**: `.github/workflows/link.yml`
- **Ansible inventory**: `ansible/inventory`
- **Compose dir on VPS**: `/opt/openclaw`
- **VPS user**: `jacks`

---

## Active Projects

*(None yet — will populate as work begins)*

---

## Decisions Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2025-01-21 | Created `context.md` for persistent memory | Low overhead, immediate benefit — can upgrade to structured JSON or DB later |

---

## Technical Debt / Known Issues

*(None logged yet)*

---

## API Notes & Quirks

*(Document platform-specific behaviours as discovered)*

---

## Conventions

- **Commit messages**: Imperative mood, concise (`Add feature X`, not `Added feature X`)
- **Branch naming**: `feature/<name>`, `fix/<name>`, `chore/<name>`
- **Destructive ops**: Always require explicit user confirmation

---

## Open Questions

- What bots/services are currently defined in the VPS docker-compose.yml?
- Is the GitHub Actions workflow (`link.yml`) already created, or do we need to bootstrap it?

---

*Last updated: 2025-01-21*
