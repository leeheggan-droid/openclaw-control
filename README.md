# openclaw-control

A minimal, Ansible-based control layer for managing Dockerised bots on an
Ubuntu VPS.  All legacy UI and application code has been removed; this
repository now contains only the automation scripts needed to operate the
Docker Compose stack remotely via SSH.

---

## Repository structure

```
openclaw-control/
├── ansible/
│   ├── inventory          # Host/SSH configuration — edit this first
│   ├── site.yml           # Root playbook (up / down / restart / status / deploy / logs)
│   ├── tasks/
│   │   ├── status.yml     # Show container status
│   │   ├── up.yml         # Start containers
│   │   ├── down.yml       # Stop containers
│   │   ├── restart.yml    # Restart containers
│   │   ├── deploy.yml     # Pull latest images + recreate containers
│   │   └── logs.yml       # Fetch recent container logs
│   └── roles/
│       └── README.md      # Guide for adding future Ansible roles
└── README.md              # This file
```

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
```

### On the VPS

| Requirement | Notes |
|---|---|
| Ubuntu | 24.04.4 LTS (Noble Numbat) — confirmed target |
| Docker CE | [Install guide](https://docs.docker.com/engine/install/ubuntu/) |
| Docker Compose v2 | Ships with Docker CE as the `docker compose` plugin |
| `docker-compose.yml` | Must exist at the path set in `ansible/inventory` (default: `/opt/openclaw`) |

---

## Setup

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
# Default output: ~/.ssh/id_ed25519  (private) and ~/.ssh/id_ed25519.pub (public)
```

Copy the public key to the VPS:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub jacks@72.61.123.4
# Confirm you can log in without a password:
ssh -i ~/.ssh/id_ed25519 jacks@72.61.123.4
```

### 4. Review `ansible/inventory`

The inventory is already configured for your server (`srv1501082` /
`72.61.123.4`, user `jacks`).  The only value you may need to update is the
path to your local private key if it differs from the default:

```ini
[vps]
srv1501082 ansible_host=72.61.123.4 ansible_user=jacks ansible_ssh_private_key_file=~/.ssh/id_ed25519 ...
```

Also update the group variables at the bottom of the file if your
`docker-compose.yml` lives in a different path on the VPS:

```ini
[vps:vars]
docker_compose_dir=/opt/openclaw      # path to docker-compose.yml on the VPS
docker_compose_project=openclaw       # Docker Compose project name
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

## Usage

All commands are run from the **repository root**.

### Check container status (default / safe)

```bash
ansible-playbook -i ansible/inventory ansible/site.yml
# or explicitly:
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

### Deploy (pull latest images + recreate containers)

Use this after pushing new Docker image versions to your registry:

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=deploy"
```

### View recent container logs

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=logs"
```

### Dry-run (check mode — no changes made)

Append `--check` to any command to simulate what Ansible *would* do without
actually executing anything on the VPS:

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=up" --check
```

### Increase verbosity for debugging

Add `-v`, `-vv`, or `-vvv` to see more detail:

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=status" -v
```

---

## Extending the playbooks

### Add a new action

1. Create a new task file in `ansible/tasks/`, e.g. `ansible/tasks/scale.yml`.
2. Add a corresponding `when: action == "scale"` block in `ansible/site.yml`
   under the `tasks:` section.

### Target a single service

Pass the service name as an extra variable and reference it in the task:

```bash
# Example — restart only the 'bot-alpha' service
ansible-playbook -i ansible/inventory ansible/site.yml \
  -e "action=restart" -e "service=bot-alpha"
```

Then edit `ansible/tasks/restart.yml` to use `{{ service | default('') }}` in
the `docker compose restart` command.

### Add more bots / services

Add new services to your `docker-compose.yml` on the VPS.  No changes are
needed in this repo — all playbooks operate on whatever services the compose
file defines.

### Add a new VPS or environment

1. Add the new host to `ansible/inventory` under an existing or new group.
2. Optionally create `ansible/group_vars/<group>.yml` to set group-specific
   variables (e.g. different `docker_compose_dir` for staging vs production).
3. Target the new host with `-l` (limit):

```bash
ansible-playbook -i ansible/inventory ansible/site.yml -e "action=status" -l staging
```

### Structured Ansible roles

As the automation grows, consider extracting common concerns into roles stored
in `ansible/roles/`.  See `ansible/roles/README.md` for guidance and
scaffolding instructions.

---

## Safety checklist

- [ ] No secrets or credentials added to source code
- [ ] No destructive operations introduced
- [ ] `ansible/inventory` contains the real VPS IP and user (explicitly configured
      for this project) — **no passwords or private keys are committed**; only the
      public connectivity details (IP, username) that are required to run playbooks
- [ ] Changes limited to the minimum required

---

## References

- [Ansible documentation](https://docs.ansible.com/)
- [Docker Compose CLI reference](https://docs.docker.com/compose/reference/)
- [ansible-galaxy role init](https://docs.ansible.com/ansible/latest/cli/ansible-galaxy.html)
