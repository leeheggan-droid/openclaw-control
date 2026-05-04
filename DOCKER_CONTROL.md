# Docker Control Runner

This repository (`openclaw-control`) can be executed from a containerised control runner.
The control runner is **ephemeral** — it runs an Ansible command and exits.
The VPS always remains the **remote target**, never the control host.

## What this solves

- Avoids host Python / Ansible installation issues (e.g. Ubuntu PEP 668).
- Makes local runs and GitHub Actions runs behave the same.
- Keeps control tooling separate from runtime bot services.
- Lets any operator with Docker + SSH access run Ansible without installing Ansible locally.

## Build the control image

Run from the repository root:

```bash
docker build -t openclaw-control:ci .
```

## Run a playbook tag

```bash
docker run --rm \
  -e ANSIBLE_HOST_KEY_CHECKING=False \
  -e ANSIBLE_SSH_ARGS="-F /dev/null" \
  -v "$PWD:/work" \
  -v "$HOME/.ssh:/root/.ssh:ro" \
  openclaw-control:ci \
  -i ansible/inventory ansible/site.yml --tags <tag>
```

Replace `<tag>` with one of: `status-all`, `systemd-status`, `systemd-logs`, `systemd-restart`.

### Example — full status check

```bash
docker run --rm \
  -e ANSIBLE_HOST_KEY_CHECKING=False \
  -e ANSIBLE_SSH_ARGS="-F /dev/null" \
  -v "$PWD:/work" \
  -v "$HOME/.ssh:/root/.ssh:ro" \
  openclaw-control:ci \
  -i ansible/inventory ansible/site.yml --tags status-all
```

## How it works

| Part | Detail |
|---|---|
| Base image | `python:3.11-slim` |
| Ansible install | `pip install ansible` (inside image) |
| Entrypoint | `ansible-playbook` |
| Working dir | `/work` (repo root mounted at runtime) |
| SSH keys | `$HOME/.ssh` mounted read-only at `/root/.ssh` |
| Inventory | `ansible/inventory` (committed; uses `~/.ssh/id_rsa` from the mount) |

The container never holds secrets.  SSH keys come from the host mount.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `ANSIBLE_HOST_KEY_CHECKING` | Set to `False` to skip known_hosts check | `True` |
| `ANSIBLE_SSH_ARGS` | Set to `-F /dev/null` to ignore host SSH config | — |

## When to use this vs other execution contexts

| Context | When to use |
|---|---|
| `LOCAL_DOCKER` | Operator has Docker + SSH key; no Ansible installed locally |
| `LOCAL_SSH` | Operator has Ansible + SSH key installed locally |
| `GITHUB_ACTIONS` | Remote trigger via GitHub UI or Link AI (no local machine needed) |
