# openclaw-control

An Ansible-based control layer for remotely managing the OpenClaw bot stack on
an Ubuntu VPS.  Operations are triggered either directly via `ansible-playbook`
from a local machine or automatically through a GitHub Actions
`workflow_dispatch` event ("Link Control").  All bots run as **native systemd
services** on the host.  The web UI (`www.leeheggan.tech`) is a separate Vercel
app (the Link repo) and is not managed here.

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

> **Note:** `www.leeheggan.tech` is served by Vercel (the Link app repo).
> There is no Docker web app or Traefik proxy on the VPS.

### How it works end-to-end

1. **Trigger** — Link (the Vercel AI assistant) or a human selects an action
   (`status-all`, `systemd-status`, `systemd-restart`, `systemd-logs`) via the
   GitHub API or GitHub UI.
2. **Task dispatch** — `site.yml` selects the matching task file from
   `ansible/tasks/` using Ansible tags.
3. **Execution** — the task file runs `systemctl` or `journalctl` directly on
   the VPS host.
4. **Output** — results are printed to the Ansible console or GitHub Actions log.

See `link/context/how-link-interacts.md` for the full end-to-end interaction
diagram including the GitHub API call format.

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
| Container orchestration | Docker Compose v2 (`docker compose` plugin) | v2+ (LinkedIn bot only) |
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
| systemd | Standard Ubuntu install |

### For GitHub Actions (CI path)

| Requirement | Notes |
|---|---|
| `VPS_SSH_KEY` secret | The **private** RSA key whose public half is installed on the VPS for user `jacks` |
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

### `action` (tag) variable

Selected at runtime via the workflow dropdown or `--tags <value>` on the
command line.  Defaults to `status-all` in the GitHub Actions workflow.

| Tag | Effect | Destructive? |
|---|---|---|
| `status-all` | Status of all systemd bots | No (default) |
| `systemd-status` | Detailed status of all 4 systemd bots | No |
| `systemd-restart` | Restart all 4 systemd bots | No |
| `systemd-logs` | journald logs for all 4 systemd bots (`tail_lines` lines) | No |

### GitHub Actions secrets

| Secret | Description |
|---|---|
| `VPS_SSH_KEY` | Contents of the private SSH key (written to `~/.ssh/id_rsa` on the runner) |
| `ANSIBLE_INVENTORY` | Full content of a valid Ansible inventory file (written to `ansible/inventory.ini` at runtime) |

---

## Operator Guide — How OpenClaw Is Started

All bots run as **native systemd services** on the VPS (`srv1501082`).  There
is no Docker web app.  The web UI (`www.leeheggan.tech`) runs entirely on
Vercel via the Link repo.  Ansible only manages systemd service operations
over SSH — it is not responsible for initial installation.

### After a code change — restart checklist

Use this checklist every time you push new code and need the running service
to pick it up.

#### openclaw-agent (GitHub Issue Polling Agent)

- [ ] SSH into the VPS: `ssh jacks@72.61.123.4`
- [ ] `cd /opt/openclaw-agent && git pull`
- [ ] `sudo systemctl restart openclaw-agent.service`
- [ ] Verify: `systemctl status openclaw-agent.service`
- [ ] Check logs: `journalctl -u openclaw-agent.service -n 20`

⚠️ Ensure `GITHUB_REPO` (and any other required env vars) are set in the
service's environment — a missing variable will cause the service to fail.
Run `systemctl cat openclaw-agent.service` to see what env vars are expected.

#### openclaw-crypto (Kraken trading bot — REAL GBP)

- [ ] **Check open positions before restarting**: verify no active trades in Kraken
- [ ] SSH into the VPS
- [ ] `cd /home/jacks/openclaw-crypto && git pull`
- [ ] `sudo systemctl restart openclaw-crypto.service`
- [ ] Verify: `systemctl status openclaw-crypto.service`

#### alpaca_orb_bite_bot (paper trading)

- [ ] SSH into the VPS
- [ ] `cd /home/jacks/alpaca_orb_bite_bot && git pull`
- [ ] `sudo systemctl restart alpaca_orb_bite_bot.service`
- [ ] Verify: `systemctl status alpaca_orb_bite_bot.service`

#### Or restart everything via GitHub Actions / Link

1. Go to **Actions → Link Control → Run workflow**
2. Select `systemd-restart`

---

## Usage

All commands below are run from the **repository root** on your local machine.

### Check status of all systemd bots

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags status-all
```

### Detailed status of individual bots

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags systemd-status
```

### Restart all systemd bots

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags systemd-restart
```

### View recent bot logs

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
4. Optionally set `tail_lines` (applies to `systemd-logs` only).
5. Click **Run workflow** to execute.

The runner installs Ansible, writes the SSH key from the `VPS_SSH_KEY` secret
to `~/.ssh/id_rsa`, writes the inventory from the `ANSIBLE_INVENTORY` secret
to `ansible/inventory.ini`, and runs `ansible/site.yml` with the selected tag.

### Via GitHub API (for Link)

```
POST /repos/leeheggan-droid/openclaw-control/actions/workflows/link.yml/dispatches
{ "ref": "main", "inputs": { "action": "systemd-restart", "tail_lines": "50" } }
```

See `link/context/how-link-interacts.md` for the full API interaction guide.

---

## Project Structure

```
openclaw-control/
│
├── ansible/
│   ├── inventory              # Ansible host/SSH configuration — edit this first
│   ├── site.yml               # Root playbook; dispatches to task files based on Ansible tags
│   └── tasks/
│       ├── status-all.yml     # systemctl status for all 4 systemd bots
│       ├── systemd-status.yml # Detailed systemctl status for all 4 systemd bots
│       ├── systemd-restart.yml# systemctl restart for all 4 systemd bots
│       └── systemd-logs.yml   # journalctl logs for all 4 systemd bots
│
├── link/
│   └── context/               # Persistent memory store for the Link AI assistant
│       ├── environment.md     # Active execution context — read this first before any operation
│       ├── decisions.md       # Architectural and operational decisions log
│       ├── projects.md        # Active projects and their status
│       ├── quirks.md          # API quirks, platform-specific gotchas, technical debt
│       ├── how-link-interacts.md  # Full Link → GitHub API → Ansible → VPS flow
│       └── services/
│           ├── host-overview.md             # Master list of all running services
│           ├── openclaw-agent.md            # GitHub Issue Polling Agent (systemd)
│           ├── openclaw-crypto.md           # Kraken crypto bot (systemd)
│           ├── alpaca-orb-bite-bot.md       # Alpaca paper trading bot (systemd)
│           └── linkedin-data-centre-news-bot.md  # LinkedIn news bot (systemd timer)
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

To restart only one bot, SSH into the VPS and run:

```bash
sudo systemctl restart openclaw-agent.service
```

Or add a targeted playbook task and call it with `--tags`.

### Adding a new VPS or environment

1. Add a host entry to `ansible/inventory` (or a new group with its own
   `[<group>:vars]` section).
2. Optionally create `ansible/group_vars/<group>.yml` for environment-specific
   variables.
3. Use the `-l` (limit) flag to target only that host or group:

```bash
ansible-playbook -i ansible/inventory ansible/site.yml --tags status-all -l staging
```

### Ansible roles

As automation grows, extract reusable concerns into roles under `ansible/roles/`
using the standard scaffolding tool:

```bash
cd ansible/
ansible-galaxy role init roles/<role-name>
```

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
