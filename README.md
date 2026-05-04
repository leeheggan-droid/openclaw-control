# openclaw-control

An Ansible-based control layer for remotely managing the OpenClaw stack on
an Ubuntu VPS.  Operations are triggered either directly via `ansible-playbook`
from a local machine or automatically through a GitHub Actions
`workflow_dispatch` event ("Link Control").  The stack is a **hybrid system**:
the main OpenClaw web app and its cron runner are Docker containers, while the
GitHub Issue Polling Agent (`openclaw-agent`) and the supporting trading bots
run as native systemd services.  All application / UI code lives elsewhere;
this repository contains only the automation needed to operate the stack over
SSH.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Tech Stack](#tech-stack)
3. [Prerequisites](#prerequisites)
4. [Installation & Setup](#installation--setup)
5. [Configuration](#configuration)
6. [Operator Guide — How OpenClaw Is Started](#operator-guide--how-openclaw-is-started)
7. [Usage](#usage)
8. [Project Structure](#project-structure)
9. [Contributing](#contributing)
10. [License](#license)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│  Control surface (one of two paths)                 │
│                                                     │
│  A) Local machine                                   │
│     ansible-playbook -i ansible/inventory …         │
│                                                     │
│  B) GitHub Actions ("Link Control" workflow)        │
│     Triggered via workflow_dispatch in the UI       │
│     or GitHub API; Ansible runs on ubuntu-latest    │
│     runner, SSH key injected from repository secret │
└────────────────────┬────────────────────────────────┘
                     │ SSH (RSA key)
                     ▼
         ┌───────────────────────────────────────────┐
         │  Ubuntu VPS — srv1501082 / 72.61.123.4    │
         │                                           │
         │  Docker Compose — /docker/openclaw-1ne6/  │
         │  ├─ openclaw-1ne6-openclaw-1  (app:43248) │
         │  ├─ openclaw-1ne6-openclaw-cron-1         │
         │  └─ traefik  (80/443)                     │
         │                                           │
         │  systemd services (native host)           │
         │  ├─ openclaw-agent.service                │
         │  │    /opt/openclaw-agent/agent.py        │
         │  ├─ openclaw-crypto.service               │
         │  │    /home/jacks/openclaw-crypto/        │
         │  ├─ openclaw-vibe-gateway.service         │
         │  ├─ alpaca_orb_bite_bot.service           │
         │  └─ linkedin-news.timer                   │
         └───────────────────────────────────────────┘
```

### How it works end-to-end

1. **Trigger** — a human (or the Link AI assistant) selects an action
   (`up`, `down`, `restart`, `status`, `deploy`, `logs`, `systemd-status`,
   `systemd-restart`, `systemd-logs`, `status-all`) and runs the playbook.
2. **Task dispatch** — `site.yml` selects the matching task file from
   `ansible/tasks/` using Ansible tags.
3. **Execution** — Docker actions run `docker compose` commands at
   `docker_compose_dir` on the VPS; `systemd-*` actions call `systemctl`
   or `journalctl` directly on the host.
4. **Output** — results are printed to the Ansible console or GitHub Actions log.

### "Link" persistent memory

The `link/context/` directory holds markdown files that serve as a persistent
memory store for the Link operational AI assistant.  These files are
human-readable, version-controlled, and loaded into context at the start of
each Link session.

**`link/context/environment.md`** is the single source of truth for the active
execution context (local SSH vs GitHub Actions).  Link must read it before
taking any operational action.  A human operator updates it whenever the
execution environment changes.

---

## Tech Stack

| Layer | Technology | Version |
|---|---|---|
| Automation engine | [Ansible](https://docs.ansible.com/) | ≥ 2.12 |
| Runtime for Ansible | Python | ≥ 3.9 |
| CI / remote trigger | GitHub Actions | — |
| Container runtime | Docker CE | latest stable |
| Container orchestration | Docker Compose v2 (`docker compose` plugin) | v2+ |
| Target OS | Ubuntu | 24.04.4 LTS (Noble Numbat) |
| SSH key type | RSA (`id_rsa`) | — |

---

## Prerequisites

### On your control machine (laptop / CI server)

| Requirement | Min version | Install |
|---|---|---|
| Python | 3.9+ | [python.org](https://www.python.org/) |
| Ansible | 2.12+ | `pip install ansible` |
| SSH client | any | usually pre-installed |

Verify:

```bash
ansible --version
python3 --version
```

### On the VPS

| Requirement | Notes |
|---|---|
| Ubuntu | 24.04.4 LTS (Noble Numbat) — confirmed target OS |
| Docker CE | [Install guide](https://docs.docker.com/engine/install/ubuntu/) |
| Docker Compose v2 | Ships with Docker CE as the `docker compose` plugin |
| `docker-compose.yml` | Must exist at the path defined by `docker_compose_dir` (default: `/opt/openclaw`) |

### For GitHub Actions (CI path)

| Requirement | Notes |
|---|---|
| `VPS_SSH_KEY` secret | The **private** ed25519 key whose public half is installed on the VPS for user `jacks` |
| `ANSIBLE_INVENTORY` secret | Full content of a valid Ansible inventory file (same format as `ansible/inventory`) |

---

## Installation & Setup

### 1. Clone this repository

```bash
git clone git@github.com:leeheggan-droid/openclaw-control.git
cd openclaw-control
```

### 2. Install Ansible

```bash
pip install ansible        # or: pip install --user ansible
ansible --version          # confirm it is on your PATH
```

### 3. Set up SSH key authentication to the VPS

The inventory uses `~/.ssh/id_rsa`.  If you do not already have an RSA key pair:

```bash
ssh-keygen -t rsa -b 4096 -C "openclaw-control"
# Output: ~/.ssh/id_rsa (private) and ~/.ssh/id_rsa.pub (public)
```

Copy the public key to the VPS:

```bash
ssh-copy-id -i ~/.ssh/id_rsa.pub jacks@72.61.123.4
# Confirm you can log in without a password:
ssh -i ~/.ssh/id_rsa jacks@72.61.123.4
```

### 4. Review `ansible/inventory`

The inventory is pre-configured for the production VPS (`srv1501082` /
`72.61.123.4`, user `jacks`).  Update the private key path if yours differs:

```ini
[vps]
srv1501082 ansible_host=72.61.123.4 ansible_user=jacks ansible_ssh_private_key_file=~/.ssh/id_rsa

[vps:vars]
ansible_python_interpreter=/usr/bin/python3
docker_compose_dir=/docker/openclaw-1ne6     # path to docker-compose.yml on the VPS
```

### 5. Test connectivity

```bash
ansible -i ansible/inventory vps -m ping
```

Expected output:

```
srv1501082 | SUCCESS => {
    "changed": false,
    "ping": "pong"
}
```

---

## Configuration

### `ansible/inventory`

The single inventory file that tells Ansible which host(s) to manage.

| Variable | Default | Description |
|---|---|---|
| `ansible_host` | `72.61.123.4` | IP address of the VPS |
| `ansible_user` | `jacks` | SSH user on the remote server |
| `ansible_ssh_private_key_file` | `~/.ssh/id_rsa` | Path to the local private key |
| `ansible_python_interpreter` | `/usr/bin/python3` | Python binary on the remote host |
| `docker_compose_dir` | `/docker/openclaw-1ne6` | Directory on the VPS containing `docker-compose.yml` |

### `action` (tag) variable

Selected at runtime via the workflow dropdown or `--tags <value>` on the
command line.  Defaults to `status-all` in the GitHub Actions workflow.

| Tag | Effect | Destructive? |
|---|---|---|
| `status-all` | Docker `ps` + all systemd bot statuses | No (default) |
| `status` | `docker compose ps` — show running containers | No |
| `systemd-status` | Status of all 4 systemd bots | No |
| `up` | `docker compose up -d` — start all services | No |
| `down` | `docker compose down` — stop and remove containers | **Yes** |
| `restart` | `docker compose restart` — restart without image change | No |
| `systemd-restart` | Restart all 4 systemd bots | No |
| `deploy` | `git pull` + `docker compose build` + `up -d` | **Yes** |
| `logs` | Docker container logs (`tail_lines` lines) | No |
| `systemd-logs` | journald logs for all 4 systemd bots (`tail_lines` lines) | No |

### GitHub Actions secrets

| Secret | Description |
|---|---|
| `VPS_SSH_KEY` | Contents of the private SSH key (written to `~/.ssh/id_rsa` on the runner) |
| `ANSIBLE_INVENTORY` | Full content of a valid Ansible inventory file (written to `ansible/inventory.ini` at runtime) |

> **Note:** No secrets or credentials are ever committed to the repository.
> The `ansible/inventory` file contains only the public connectivity details
> (IP address and username).

---

## Operator Guide — How OpenClaw Is Started

OpenClaw runs as a **hybrid stack** on a single Ubuntu VPS (`srv1501082`).
The main web application and its scheduled-task runner are managed by **Docker
Compose** at `/docker/openclaw-1ne6/` (containers `openclaw-1ne6-openclaw-1`,
`openclaw-1ne6-openclaw-cron-1`, and the Traefik reverse proxy).  Separately,
the **GitHub Issue Polling Agent** (`openclaw-agent.service`) and the trading
bots (`openclaw-crypto.service`, `alpaca_orb_bite_bot.service`) run as
**native systemd services** directly on the host — there is no Docker involved
for those processes.  A `linkedin-news.timer` systemd unit fires the LinkedIn
news bot on a weekly schedule.  There is **no** `docker-compose` CLI binary
(the stack uses the `docker compose` plugin), no standalone Dockerfile in this
control repo, and Ansible is not responsible for initial installation — it only
operates an already-running stack over SSH.

### After a code change — restart checklist

Use this checklist every time you do a `git pull` (or push new code) and need
the running system to pick up the changes.

#### Openclaw web app (Docker)

- [ ] SSH into the VPS: `ssh jacks@72.61.123.4`
- [ ] `cd /docker/openclaw-1ne6`
- [ ] `git pull` (if the compose project tracks a repo; otherwise skip)
- [ ] `docker compose build` — rebuild the image with new code
- [ ] `docker compose up -d` — recreate containers from the new image
- [ ] Verify: `docker ps` and `docker compose logs --tail=20`

Or via GitHub Actions: **Actions → Link Control → Run workflow → `deploy`**

#### openclaw-agent (systemd — GitHub Issue Polling Agent)

- [ ] SSH into the VPS: `ssh jacks@72.61.123.4`
- [ ] `cd /opt/openclaw-agent && git pull`
- [ ] `sudo systemctl restart openclaw-agent.service`
- [ ] Verify: `systemctl status openclaw-agent.service`
- [ ] Check logs: `journalctl -u openclaw-agent.service -n 20`

⚠️ Ensure `GITHUB_REPO` (and any other required env vars) are set in the
service's environment — a missing variable will cause the service to fail.
Run `systemctl cat openclaw-agent.service` to see what env vars are expected.

#### openclaw-crypto (systemd — Kraken trading bot, REAL GBP)

- [ ] **Check open positions before restarting**: verify no active trades in Kraken
- [ ] SSH into the VPS
- [ ] `cd /home/jacks/openclaw-crypto && git pull`
- [ ] `sudo systemctl restart openclaw-crypto.service`
- [ ] Verify: `systemctl status openclaw-crypto.service`

#### alpaca_orb_bite_bot (systemd — paper trading)

- [ ] SSH into the VPS
- [ ] `cd /home/jacks/alpaca_orb_bite_bot && git pull`
- [ ] `sudo systemctl restart alpaca_orb_bite_bot.service`
- [ ] Verify: `systemctl status alpaca_orb_bite_bot.service`

#### Or restart everything via GitHub Actions

1. Go to **Actions → Link Control → Run workflow**
2. Select `systemd-restart` to restart all 4 systemd bots, or `deploy` to
   rebuild and restart the Docker stack.

---

## Usage

All commands below are run from the **repository root** on your local machine.

### Check full server status (Docker + all systemd bots)

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags status-all
```

### Check Docker container status only

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags status
```

### Start Docker containers

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags up
```

### Stop Docker containers

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags down
```

### Restart Docker containers

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags restart
```

### Restart all systemd bots

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags systemd-restart
```

### Deploy — rebuild Docker image and recreate containers

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags deploy
```

### View recent Docker container logs

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags logs -e "tail_lines=50"
```

### View recent systemd bot logs

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags systemd-logs -e "tail_lines=50"
```

### Increase verbosity for debugging

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags status-all -v
# -vv or -vvv for even more detail
```

### Via GitHub Actions (Link Control workflow)

1. Navigate to **Actions → Link Control** in the GitHub UI.
2. Click **Run workflow**.
3. Select the desired action from the dropdown.
4. Optionally set `tail_lines` (applies to `logs` and `systemd-logs` only).
5. Click **Run workflow** to execute.

The runner installs Ansible, writes the SSH key from the `VPS_SSH_KEY` secret
to `~/.ssh/id_rsa`, writes the inventory from the `ANSIBLE_INVENTORY` secret
to `ansible/inventory.ini`, and runs `ansible/site.yml` with the selected tag.

---

## Project Structure

```
openclaw-control/
│
├── ansible/
│   ├── inventory              # Ansible host/SSH configuration — edit this first
│   ├── site.yml               # Root playbook; dispatches to task files based on Ansible tags
│   ├── tasks/
│   │   ├── status-all.yml     # Docker ps + all systemd bot statuses
│   │   ├── status.yml         # docker compose ps
│   │   ├── up.yml             # docker compose up -d
│   │   ├── down.yml           # docker compose down
│   │   ├── restart.yml        # docker compose restart
│   │   ├── deploy.yml         # git pull + docker compose build + up -d
│   │   ├── logs.yml           # docker compose logs
│   │   ├── systemd-status.yml # systemctl status for all 4 systemd bots
│   │   ├── systemd-restart.yml# systemctl restart for all 4 systemd bots
│   │   └── systemd-logs.yml   # journalctl logs for all 4 systemd bots
│   └── roles/
│       └── README.md          # Guide for scaffolding future Ansible roles
│
├── link/
│   └── context/               # Persistent memory store for the Link AI assistant
│       ├── environment.md     # Active execution context — read this first before any operation
│       ├── decisions.md       # Architectural and operational decisions log
│       ├── projects.md        # Active projects and their status
│       ├── quirks.md          # API quirks, platform-specific gotchas, technical debt
│       └── services/
│           ├── host-overview.md          # Master list of all running services
│           ├── openclaw-agent.md         # GitHub Issue Polling Agent (systemd)
│           ├── openclaw-crypto.md        # Kraken crypto bot (systemd)
│           ├── alpaca-orb-bite-bot.md    # Alpaca paper trading bot (systemd)
│           └── linkedin-data-centre-news-bot.md  # LinkedIn news bot (Docker)
│
├── .github/
│   ├── workflows/
│   │   └── link.yml           # "Link Control" workflow_dispatch CI trigger
│   ├── ISSUE_TEMPLATE/
│   │   └── copilot_task.md    # Issue template for Copilot-handled tasks
│   └── copilot-instructions.md # Standing instructions for the GitHub Copilot agent
│
├── context.md                 # Top-level system overview for the Link assistant
└── README.md                  # This file
```

---

## Contributing

### Branching

| Pattern | Purpose |
|---|---|
| `feature/<name>` | New functionality |
| `fix/<name>` | Bug fixes |
| `chore/<name>` | Maintenance, documentation, refactoring |

### Commit messages

Use the imperative mood and keep the subject line concise:
`Add logs task` not `Added logs task`.

### Adding a new action

1. Create `ansible/tasks/<action>.yml` following the pattern of existing task
   files.
2. Add an `- import_playbook: tasks/<action>.yml` entry with a matching
   `tags: [<action>]` block in `ansible/site.yml`.
3. Add the new value to the `options:` list in `.github/workflows/link.yml`.
4. Document the new action in the tag table in this README.

### Targeting a single service

Pass the service name as an extra variable:

```bash
ansible-playbook -i ansible/inventory ansible/site.yml \
  -e "action=restart" -e "service=bot-alpha"
```

Then update the relevant task file to use `{{ service | default('') }}` in the
`docker compose` command.

### Adding a new VPS or environment

1. Add a host entry to `ansible/inventory` (or a new group with its own
   `[<group>:vars]` section).
2. Optionally create `ansible/group_vars/<group>.yml` for environment-specific
   variables.
3. Use the `-l` (limit) flag to target only that host or group:

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=status" -l staging
```

### Ansible roles

As automation grows, extract reusable concerns into roles under `ansible/roles/`
using the standard scaffolding tool:

```bash
cd ansible/
ansible-galaxy role init roles/<role-name>
```

See `ansible/roles/README.md` for a full walkthrough and example role ideas.

### Safety checklist for pull requests

- [ ] No secrets or credentials added to source code
- [ ] No destructive operations introduced
- [ ] `ansible/inventory` contains only public connectivity details (IP, username)
      — no passwords or private keys committed
- [ ] Changes limited to the minimum required by the issue

---

## License

No licence file is present in this repository.
[TODO: verify] Confirm the intended license and add a `LICENSE` file.

---

## References

- [Ansible documentation](https://docs.ansible.com/)
- [Docker Compose CLI reference](https://docs.docker.com/compose/reference/)
- [ansible-galaxy role init](https://docs.ansible.com/ansible/latest/cli/ansible-galaxy.html)
- [GitHub Actions — workflow_dispatch](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#workflow_dispatch)
