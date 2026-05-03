# openclaw-control

An Ansible-based control layer for remotely managing a Dockerised bot stack on
an Ubuntu VPS.  Operations are triggered either directly via `ansible-playbook`
from a local machine or automatically through a GitHub Actions
`workflow_dispatch` event ("Link Control").  All application / UI code lives
elsewhere; this repository contains only the automation needed to operate the
Docker Compose stack over SSH.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Tech Stack](#tech-stack)
3. [Prerequisites](#prerequisites)
4. [Installation & Setup](#installation--setup)
5. [Configuration](#configuration)
6. [Usage](#usage)
7. [Project Structure](#project-structure)
8. [Contributing](#contributing)
9. [License](#license)

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
                     │ SSH (ed25519 key)
                     ▼
         ┌───────────────────────┐
         │  Ubuntu 24.04 VPS     │
         │  srv1501082           │
         │  72.61.123.4          │
         │                       │
         │  /opt/openclaw/       │
         │  └─ docker-compose.yml│
         │     (bot services)    │
         │                       │
         │  Docker CE + Compose  │
         └───────────────────────┘
```

### How it works end-to-end

1. **Trigger** — a human (or the Link AI assistant) selects an action
   (`up`, `down`, `restart`, `status`, `deploy`, `logs`) and runs the playbook.
2. **Pre-flight** — `site.yml` connects to the VPS via SSH, confirms the target
   OS is Ubuntu 24.04+, verifies Docker and Docker Compose are installed, and
   checks that `docker-compose.yml` exists at `docker_compose_dir`.
3. **Task dispatch** — based on the `action` variable, `site.yml` includes the
   matching task file from `ansible/tasks/`.
4. **Execution** — the task file runs the appropriate `docker compose` command
   in `docker_compose_dir` on the VPS and prints the output back to the Ansible
   console / GitHub Actions log.

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
| SSH key type | ed25519 | — |

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

If you do not already have a key pair:

```bash
ssh-keygen -t ed25519 -C "openclaw-control"
# Default output: ~/.ssh/id_ed25519 (private) and ~/.ssh/id_ed25519.pub (public)
```

Copy the public key to the VPS:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub jacks@72.61.123.4
# Confirm you can log in without a password:
ssh -i ~/.ssh/id_ed25519 jacks@72.61.123.4
```

### 4. Review `ansible/inventory`

The inventory is pre-configured for the production VPS (`srv1501082` /
`72.61.123.4`, user `jacks`).  Update the private key path if yours differs:

```ini
[vps]
srv1501082 ansible_host=72.61.123.4 \
           ansible_user=jacks \
           ansible_ssh_private_key_file=~/.ssh/id_ed25519 \
           ansible_python_interpreter=/usr/bin/python3

[vps:vars]
docker_compose_dir=/opt/openclaw     # path to docker-compose.yml on the VPS
docker_compose_project=openclaw      # Docker Compose project name
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
| `ansible_ssh_private_key_file` | `~/.ssh/id_ed25519` | Path to the local private key |
| `ansible_python_interpreter` | `/usr/bin/python3` | Python binary on the remote host |
| `docker_compose_dir` | `/opt/openclaw` | Directory on the VPS containing `docker-compose.yml` |
| `docker_compose_project` | `openclaw` | Docker Compose project name (matches `name:` in `docker-compose.yml`) |

### `action` variable

Passed at runtime via `-e "action=<value>"`.  Defaults to `status` when
omitted.  Valid values:

| Value | Effect | Destructive? |
|---|---|---|
| `status` | `docker compose ps` — show running containers | No |
| `up` | `docker compose up -d` — start all services | No |
| `down` | `docker compose down` — stop and remove containers | **Yes** |
| `restart` | `docker compose restart` — restart without image change | No |
| `deploy` | `docker compose pull` + `docker compose up -d --remove-orphans` | **Yes** |
| `logs` | `docker compose logs --tail=100 --no-color` — last 100 log lines | No |

### GitHub Actions secrets

| Secret | Description |
|---|---|
| `VPS_SSH_KEY` | Contents of the private SSH key (written to `~/.ssh/id_ed25519` on the runner) |
| `ANSIBLE_INVENTORY` | Full content of an Ansible inventory file (written to `ansible/inventory.ini` at runtime) |

> **Note:** No secrets or credentials are ever committed to the repository.
> The `ansible/inventory` file contains only the public connectivity details
> (IP address and username).

---

## Usage

All commands below are run from the **repository root** on your local machine.

### Check container status (default / safe)

```bash
ansible-playbook -i ansible/inventory ansible/site.yml
# equivalent to:
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=status"
```

### Start containers

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=up"
```

### Stop containers

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=down"
```

### Restart containers

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=restart"
```

### Deploy — pull latest images and recreate containers

Use after pushing new Docker image versions to your registry:

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=deploy"
```

### View recent container logs

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=logs"
```

### Dry-run (check mode — no changes applied)

Append `--check` to simulate what Ansible *would* do without touching the VPS:

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=up" --check
```

### Increase verbosity for debugging

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=status" -v
# -vv or -vvv for even more detail
```

### Via GitHub Actions (Link Control workflow)

1. Navigate to **Actions → Link Control** in the GitHub UI.
2. Click **Run workflow**.
3. Select the desired task from the dropdown (`up`, `down`, `restart`, `status`,
   `deploy`, `logs`).
4. Click **Run workflow** to execute.

The runner installs Ansible, writes the SSH key from the `VPS_SSH_KEY` secret,
writes the inventory from the `ANSIBLE_INVENTORY` secret, and runs the
playbook.

> [TODO: verify] `.github/workflows/link.yml` currently passes
> `-e "task=…"` but `ansible/site.yml` expects `-e "action=…"` — confirm
> this variable name is consistent between the two files.

> [TODO: verify] `.github/workflows/link.yml` writes the inventory to
> `ansible/inventory.ini` but the `ansible-playbook` command in that same
> workflow does not pass `-i ansible/inventory.ini` — confirm the inventory
> path referenced in the workflow is correct.

---

## Project Structure

```
openclaw-control/
│
├── ansible/
│   ├── inventory              # Ansible host/SSH configuration — edit this first
│   ├── site.yml               # Root playbook; dispatches to task files based on `action`
│   ├── tasks/
│   │   ├── status.yml         # docker compose ps
│   │   ├── up.yml             # docker compose up -d  (+ post-up status check)
│   │   ├── down.yml           # docker compose down
│   │   ├── restart.yml        # docker compose restart  (+ post-restart status check)
│   │   ├── deploy.yml         # docker compose pull + up -d --remove-orphans  (+ post-deploy check)
│   │   └── logs.yml           # docker compose logs --tail=100
│   └── roles/
│       └── README.md          # Guide for scaffolding future Ansible roles
│
├── link/
│   └── context/               # Persistent memory store for the Link AI assistant
│       ├── environment.md     # Active execution context — read this first before any operation
│       ├── decisions.md       # Architectural and operational decisions log
│       ├── projects.md        # Active projects and their status
│       └── quirks.md          # API quirks, platform-specific gotchas, technical debt
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
2. Add a `when: action == "<action>"` block in the `tasks:` section of
   `ansible/site.yml`.
3. Add the new value to the `options:` list in `.github/workflows/link.yml`.
4. Document the new action in the `action` variable table in this README.

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
