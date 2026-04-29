# Vibe SSH Gateway

A containerised, repo-managed SSH gateway that lets an external agent run
[vibe](https://github.com/mistralai/vibe) on a VPS without interactive shell
access.  Prompts are transported as base64 to eliminate quoting hazards.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Repository layout](#repository-layout)
3. [Prerequisites](#prerequisites)
4. [Server setup](#server-setup)
   1. [Generate the agent key](#1-generate-the-agent-key)
   2. [Run the installer](#2-run-the-installer)
   3. [Build the Docker image](#3-build-the-docker-image)
   4. [Edit the workdir allowlist](#4-edit-the-workdir-allowlist)
5. [Configuration](#configuration)
6. [Client usage](#client-usage)
7. [Security model](#security-model)
8. [Validation and testing](#validation-and-testing)
9. [Troubleshooting](#troubleshooting)

---

## Architecture

```
Agent / Control Plane                     VPS
────────────────────────                  ──────────────────────────────────────
ssh -i key openclaw-vibe@HOST             sshd
  "OPENCLAW_PROMPT_B64=<b64>              → forced-command.sh
   vibe --workdir /srv/work -p __B64__"     • validate command shape
                                             • check workdir allowlist
                                             • base64-decode prompt
                                             • acquire /run/lock/openclaw-vibe.lock
                                           → run-vibe-container.sh --workdir /srv/work
                                             → docker run \
                                                 --rm --cap-drop=ALL \
                                                 --security-opt=no-new-privileges \
                                                 -e OPENCLAW_PROMPT_B64=<b64> \
                                                 --mount src=/srv/work,dst=/work \
                                                 openclaw-vibe-gateway:latest
                                               → entrypoint.sh
                                                 → vibe --workdir /work --prompt "<decoded>"
```

The agent's SSH key is locked to a **single forced command**
(`forced-command.sh`).  No interactive shell, no port forwarding, no other
commands are possible.

---

## Repository layout

```
vibe-gateway/
├── README.md                         This file
├── bin/
│   ├── forced-command.sh             Server-side SSH forced command
│   ├── run-vibe-container.sh         Docker runner (called by forced-command)
│   └── install-server.sh            One-shot VPS installer (run as root)
├── config/
│   └── allowed-workdirs.txt         Allowlist of mountable host directories
├── docker/
│   ├── Dockerfile                   Image: Ubuntu 24.04 + vibe + non-root user
│   └── entrypoint.sh               B64-decode prompt → exec vibe
└── systemd/
    └── openclaw-vibe-gateway.service  Optional: rebuild/pull image at boot
```

---

## Prerequisites

| Where | Requirement |
|---|---|
| VPS | Ubuntu 24.04, Docker installed, `util-linux` (for `flock`) |
| VPS | Port 22 open |
| Control plane | `ssh`, `base64`, `openssh-client` |

---

## Server setup

### 1 — Generate the agent key

Run on the **control plane** (or wherever the agent will live):

```bash
ssh-keygen -t ed25519 -C "openclaw-vibe-agent" \
    -f ~/.ssh/openclaw_vibe_ed25519 -N ""
```

This creates:
- `~/.ssh/openclaw_vibe_ed25519`   — private key (keep secret)
- `~/.ssh/openclaw_vibe_ed25519.pub` — public key (copied to VPS)

### 2 — Run the installer

Copy the repo to the VPS and run the installer as root:

```bash
# On VPS — clone or copy the repo, then:
cd /opt/openclaw/openclaw-control   # or wherever the repo lives
sudo bash vibe-gateway/bin/install-server.sh \
    --pubkey-file /tmp/openclaw_vibe_ed25519.pub
```

If `--pubkey-file` is omitted you will be prompted to paste the key.

The installer:
- Creates the `openclaw-vibe` system user (home `=/var/lib/openclaw-vibe`)
- Installs scripts under `/opt/openclaw/gateway/`
- Creates `/var/lib/openclaw-vibe/cache/` for persistent vibe config
- Writes the `authorized_keys` line with the forced-command restriction
- Optionally installs the systemd unit

### 3 — Build the Docker image

```bash
# On the VPS, from the repo root:
docker build -t openclaw-vibe-gateway:latest vibe-gateway/docker/
```

### 4 — Edit the workdir allowlist

```bash
sudo nano /opt/openclaw/gateway/config/allowed-workdirs.txt
```

Add one absolute path per line — these are the **only** directories vibe is
allowed to operate in.  Entries may be exact paths or directory prefixes
(any path starting with a listed entry is allowed).

```
# Example
/srv/openclaw-work
/opt/openclaw-crypto
```

### 5 — Register the VPS host key on the control plane

```bash
ssh-keyscan -H <VPS_IP> >> ~/.ssh/known_hosts
```

---

## Configuration

### Container resource limits

Set these environment variables before invoking `run-vibe-container.sh`
(or export them in the systemd drop-in / shell profile on the VPS):

| Variable | Default | Purpose |
|---|---|---|
| `OPENCLAW_VIBE_IMAGE` | `openclaw-vibe-gateway:latest` | Docker image tag |
| `OPENCLAW_VIBE_CACHE_DIR` | `/var/lib/openclaw-vibe/cache` | Host path for persistent vibe cache (bind-mounted to `/home/vibeuser/.cache/vibe`) |
| `OPENCLAW_VIBE_SSH_DIR` | `/var/lib/openclaw-vibe/.ssh` | Host `.ssh` directory bind-mounted read-only into the container so vibe can reach `openclaw-readonly@localhost` for probe actions |
| `OPENCLAW_VIBE_PIDS_LIMIT` | `64` | Max processes in container |
| `OPENCLAW_VIBE_MEMORY` | `2g` | Container memory limit |
| `OPENCLAW_VIBE_CPUS` | `1.0` | Container CPU quota |
| `OPENCLAW_VIBE_CONTAINER_USER` | `1500:1500` | UID:GID to run as inside container — must match the `openclaw-vibe` host user UID (1500) so 0600 key files are readable |

### Model / API keys

Pass model credentials as environment variables from the **agent side**
(they are forwarded into the container if set on the host):

| Variable | Used by |
|---|---|
| `MISTRAL_API_KEY` | Mistral / vibe default backend |
| `OPENAI_API_KEY` | OpenAI backend |
| `ANTHROPIC_API_KEY` | Anthropic backend |
| `VIBE_MODEL` | Override model name |

**Never hardcode API keys** — pass them via the SSH environment or a server-side
`/etc/environment` / systemd `EnvironmentFile`.

To pass a key from the control plane over SSH, add it to `SendEnv` in
`~/.ssh/config` and `AcceptEnv` in `/etc/ssh/sshd_config` on the VPS:

```
# ~/.ssh/config (control plane)
Host <VPS_IP>
    SendEnv MISTRAL_API_KEY
```

```
# /etc/ssh/sshd_config (VPS) — add / uncomment
AcceptEnv MISTRAL_API_KEY
```

---

## Client usage

### Minimal one-liner

```bash
PROMPT="Fix the stop-loss guard in risk.py"
PROMPT_B64=$(printf '%s' "$PROMPT" | base64 -w0)

ssh -i ~/.ssh/openclaw_vibe_ed25519 \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=yes \
    -o IdentitiesOnly=yes \
    openclaw-vibe@<VPS_IP> \
    "OPENCLAW_PROMPT_B64=${PROMPT_B64} vibe --workdir /srv/openclaw-work -p __B64__"
```

### Using `bin/vibe-openclaw` (control-plane shim)

The existing `bin/vibe-openclaw` script can be adapted to use the gateway.
Set the SSH target:

```bash
export OPENCLAW_SSH_HOST="openclaw-vibe@<VPS_IP>"
export OPENCLAW_VIBE_WORKDIR="/srv/openclaw-work"

bin/vibe-openclaw --workdir /srv/openclaw-work --prompt "Fix the stop-loss guard"
```

### `authorized_keys` line format

The installer writes this line automatically.  For reference, the format is:

```
command="/opt/openclaw/gateway/bin/forced-command.sh",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA…<your-full-public-key-here>… openclaw-vibe-agent
```

Replace `AAAA…<your-full-public-key-here>…` with the full contents of your
`openclaw_vibe_ed25519.pub` file (one long line starting with `ssh-ed25519`).

---

## Security model

| Control | Mechanism |
|---|---|
| No interactive shell | `no-pty` + `command=` in `authorized_keys` |
| No port/agent forwarding | `no-port-forwarding,no-agent-forwarding,no-X11-forwarding` |
| Command shape enforcement | `forced-command.sh` validates exact token sequence |
| Workdir allowlist | Checked against `/opt/openclaw/gateway/config/allowed-workdirs.txt` |
| No path traversal | `..` is rejected in `--workdir` |
| Prompt quoting-safe | Prompt in `OPENCLAW_PROMPT_B64`; sentinel `__B64__` on the command line |
| No eval of user input | `base64 --decode` piped to a variable; never passed to `bash -c` or `eval` |
| Concurrency limit | `flock` on `/run/lock/openclaw-vibe.lock` |
| Runtime cap | `timeout 900` wraps the container runner |
| Container isolation | `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--read-only` |
| Resource limits | `--pids-limit`, `--memory`, `--cpus` |
| Non-root in container | `vibeuser` (UID 1500) inside container |

---

## Validation and testing

### Local smoke-test (no Docker required)

```bash
# 1. Export the env vars the forced command would check.
export SSH_ORIGINAL_COMMAND="vibe --workdir /tmp -p __B64__"
export OPENCLAW_PROMPT_B64="$(printf 'echo hello' | base64 -w0)"

# 2. Point forced-command at /tmp as the allowlist and VIBE_BIN to a stub.
sudo mkdir -p /opt/openclaw/gateway/config
printf '/tmp\n' | sudo tee /opt/openclaw/gateway/config/allowed-workdirs.txt

# 3. Temporarily replace the runner with a stub.
sudo install -d /opt/openclaw/gateway/bin
printf '#!/bin/bash\necho "runner called: %s\n" "$@"\n' \
    | sudo tee /opt/openclaw/gateway/bin/run-vibe-container.sh
sudo chmod +x /opt/openclaw/gateway/bin/run-vibe-container.sh

# 4. Run the forced command directly.
bash vibe-gateway/bin/forced-command.sh
```

### End-to-end localhost SSH test

```bash
# On the VPS, after running install-server.sh and building the image:

# Add VPS host key to known_hosts (if not already there).
ssh-keyscan -H 127.0.0.1 >> ~/.ssh/known_hosts

PROMPT="echo hello from vibe"
PROMPT_B64=$(printf '%s' "$PROMPT" | base64 -w0)

ssh -i /root/.ssh/openclaw_vibe_ed25519 \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=yes \
    -o IdentitiesOnly=yes \
    openclaw-vibe@127.0.0.1 \
    "OPENCLAW_PROMPT_B64=${PROMPT_B64} vibe --workdir /srv/openclaw-work -p __B64__"

echo "exit=$?"
```

Expected: exit 0; vibe runs inside the container and outputs to stdout.

### Rejection tests

```bash
# No command → rejected.
ssh ... openclaw-vibe@HOST   # "no command provided"

# Wrong command → rejected.
ssh ... openclaw-vibe@HOST "whoami"

# Bad workdir (not in allowlist) → rejected.
ssh ... openclaw-vibe@HOST \
    "OPENCLAW_PROMPT_B64=... vibe --workdir /etc -p __B64__"

# Missing OPENCLAW_PROMPT_B64 → rejected.
ssh ... openclaw-vibe@HOST \
    "vibe --workdir /srv/openclaw-work -p __B64__"
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `DENIED: no command provided` | SSH without a command | Always pass a command string |
| `DENIED: command must be 'vibe'` | Wrong command format | Use `vibe --workdir … -p __B64__` |
| `DENIED: workdir '…' is not in the allowlist` | Path not in allowlist | Add it to `/opt/openclaw/gateway/config/allowed-workdirs.txt` |
| `DENIED: OPENCLAW_PROMPT_B64 … not set` | Env var not forwarded | Include `OPENCLAW_PROMPT_B64=…` in the SSH command string |
| `DENIED: --prompt value must be '__B64__'` | Literal prompt in command | Use `__B64__` sentinel; put real prompt in `OPENCLAW_PROMPT_B64` |
| `another vibe run is already in progress` | Concurrent run | Wait for the previous run to finish. If no vibe process is running, verify with `fuser /run/lock/openclaw-vibe.lock`; if the file is truly stale, remove it: `rm /run/lock/openclaw-vibe.lock` |
| `runner not found or not executable` | install-server not run | Run `install-server.sh` again |
| Container exits immediately | Docker not installed, or image not built | `docker build -t openclaw-vibe-gateway:latest vibe-gateway/docker/` |
| `bad interpreter: Permission denied` | vibe installed under /home | Re-install vibe system-wide inside the Docker image (see Dockerfile) |
| `Permission denied (publickey,password)` on vibe SSH actions (e.g. `uptime`, `ls`) | `openclaw-vibe` has no outbound SSH identity to reach `openclaw-readonly@localhost` | Re-run `install-server.sh` (step 6 generates the outbound key); or follow `docs/ssh-execution-gateway.md` §11 |
