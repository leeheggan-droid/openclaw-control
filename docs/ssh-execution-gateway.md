# SSH Execution Gateway — Setup Guide

This document provides copy/pasteable commands for the full end-to-end setup of
the OpenClaw → VPS SSH execution gateway, from user creation through key
generation to smoke-testing.

The gateway is split into **two independent SSH lanes**:

| Lane | Env var | User | Wrapper | Used for |
|------|---------|------|---------|---------|
| **Vibe (execution)** | `OPENCLAW_SSH_HOST` | `openclaw-vibe` | `bin/vibe-forced-command.sh` | Running `vibe` tasks; mutative operations |
| **READONLY (probes)** | `OPENCLAW_SSH_READONLY_HOST` | `openclaw-readonly` | `bin/vibe-readonly-wrapper.sh` | Terminal pills, autopilot probes, snapshots, `/ops/report` |

**No fallback**: if `OPENCLAW_SSH_READONLY_HOST` is unset, all read-only features
are disabled gracefully. They will never silently use `OPENCLAW_SSH_HOST`.

---

## Overview

```
Control plane
  ├─ VIBE lane (execution)
  │    └─ ssh openclaw-vibe@<VPS>
  │         └─ bin/vibe-forced-command.sh (forced-command)
  │              └─ /usr/local/bin/vibe --workdir <path> --prompt <text>
  │
  └─ READONLY lane (probes / terminal pills / autopilot)
       └─ ssh openclaw-readonly@<VPS>
            └─ bin/vibe-readonly-wrapper.sh (forced-command)
                 └─ bash -c "<allowlisted-command>"
```

---

## 1 — Create the `openclaw-vibe` system user on the VPS

```bash
# Create the user with a stable home directory under /var/lib/
sudo useradd \
    --system \
    --shell /bin/bash \
    --home-dir /var/lib/openclaw-vibe \
    --create-home \
    openclaw-vibe

# Verify — home must be /var/lib/openclaw-vibe, NOT /home/openclaw-vibe
sudo getent passwd openclaw-vibe
# Expected: openclaw-vibe:x:<uid>:<gid>::/var/lib/openclaw-vibe:/bin/bash
```

> **Note:** If the user already exists but has a different home directory you
> can check and correct it with:
> ```bash
> sudo usermod --home /var/lib/openclaw-vibe --move-home openclaw-vibe
> ```

---

## 2 — Install the forced-command wrapper on the VPS

```bash
# Create the installation directory
sudo install -d -m 0755 -o root -g root /opt/openclaw/bin

# Copy the wrapper from the repo (run from repo root)
sudo install -m 0755 -o root -g root \
    bin/vibe-forced-command.sh \
    /opt/openclaw/bin/vibe-forced-command.sh

# Verify
sudo ls -la /opt/openclaw/bin/vibe-forced-command.sh
# Expected: -rwxr-xr-x 1 root root ... /opt/openclaw/bin/vibe-forced-command.sh
```

---

## 3 — Ensure a system-visible `vibe` binary at `/usr/local/bin/vibe`

The forced-command wrapper calls `/usr/local/bin/vibe` explicitly to avoid
depending on the SSH user's dotfiles or `$PATH`.  The `vibe` CLI must therefore
be installed to that path — and the binary's shebang interpreter must also be
reachable by the `openclaw-vibe` system account (i.e. NOT under any user's
`/home/...` tree).

> **⚠️  Do NOT copy a `vibe` binary from another user's home directory.**
> A `vibe` installed via `uv` for a regular user (e.g. `jacks`) is a thin
> entrypoint whose shebang line points into `/home/jacks/.local/share/uv/...`.
> Copying that file to `/usr/local/bin/vibe` preserves the bad shebang;
> `openclaw-vibe` cannot read `/home/jacks/...` and the kernel returns
> `bad interpreter: Permission denied` when the wrapper tries to exec it.
> The wrapper preflights this and refuses to launch in this case (see Verify
> below), but the right fix is to install via Option A or Option B.

Use one of the methods below — both produce a root-owned entrypoint with a
shebang that does not depend on any user home directory.

### Option A — Install vibe into a virtualenv owned by root (recommended)

```bash
sudo python3 -m venv /opt/vibe-venv
sudo /opt/vibe-venv/bin/pip install vibe   # adjust package name as needed
sudo ln -sf /opt/vibe-venv/bin/vibe /usr/local/bin/vibe
```

This produces a `vibe` whose shebang is `#!/opt/vibe-venv/bin/python3` —
root-owned, world-readable, no `/home/<user>` dependency.

### Option B — Write a shim that delegates to a versioned install

```bash
sudo tee /usr/local/bin/vibe >/dev/null <<'EOF'
#!/usr/bin/env bash
exec /opt/vibe-venv/bin/vibe "$@"
EOF
sudo chmod 0755 /usr/local/bin/vibe
```

The shebang is `/usr/bin/env bash` (system-wide, always readable) and the
delegate is the same root-owned venv from Option A.

### Verify

```bash
# 1. Wrapper sanity — must succeed as the openclaw-vibe service user
sudo -u openclaw-vibe /usr/local/bin/vibe --version
# Expected: prints vibe version string.
# If you see "bad interpreter: Permission denied", "exec format error", or the
# wrapper's own diagnostic "vibe interpreter '/home/…' lives under /home",
# the entrypoint's shebang points into a user home dir — re-install via
# Option A above.

# 2. Confirm the shebang interpreter is system-visible (no /home/...)
sudo head -1 /usr/local/bin/vibe
# Expected: shebang interpreter path that does NOT start with /home/
# Examples that are fine:
#   #!/opt/vibe-venv/bin/python3
#   #!/usr/bin/env bash
# Example that will fail at the wrapper preflight:
#   #!/home/jacks/.local/share/uv/python/cpython-3.13.0-linux-x86_64-gnu/bin/python3

# 3. Confirm the interpreter is readable+executable for openclaw-vibe
interp="$(sudo head -1 /usr/local/bin/vibe | sed -n 's|^#!\([^[:space:]]*\).*|\1|p')"
if [[ -n "$interp" ]]; then
    if sudo -u openclaw-vibe test -r "$interp" -a -x "$interp"; then
        echo "OK: $interp readable+executable for openclaw-vibe"
    else
        echo "FAIL: vibe shebang interpreter '$interp' not usable by openclaw-vibe"
    fi
fi
```

> **Why this matters:** if the entrypoint's shebang points into a user's
> `/home/...` tree, the kernel cannot resolve the interpreter as the
> `openclaw-vibe` service user (home directories are typically `0700` for the
> owning user, so the system account cannot traverse them).  The forced-
> command wrapper preflights this and exits with a clear `lives under /home`
> error, but installing correctly per Option A or Option B avoids the failure
> entirely.

---

## 4 — Generate a root-owned ED25519 key on the control plane

```bash
# Run as root on the control plane
sudo ssh-keygen \
    -t ed25519 \
    -C "openclaw-vibe" \
    -f /root/.ssh/openclaw_vibe_ed25519 \
    -N ""

# Confirm both files exist
sudo ls -la /root/.ssh/openclaw_vibe_ed25519 /root/.ssh/openclaw_vibe_ed25519.pub
# Expected:
#   -rw------- 1 root root ... /root/.ssh/openclaw_vibe_ed25519
#   -rw-r--r-- 1 root root ... /root/.ssh/openclaw_vibe_ed25519.pub
```

---

## 5 — Install the public key into the VPS `authorized_keys`

The key must live under the user's **actual** home directory
(`/var/lib/openclaw-vibe`), not `/home/openclaw-vibe`.

```bash
# On the VPS — ensure the .ssh directory exists with correct permissions
sudo install -d -m 0700 -o openclaw-vibe -g openclaw-vibe \
    /var/lib/openclaw-vibe/.ssh

# Copy the public key from the control plane to the VPS
# (replace <VPS_HOST> with the actual hostname/IP)
pubkey="$(sudo cat /root/.ssh/openclaw_vibe_ed25519.pub)"

sudo tee /var/lib/openclaw-vibe/.ssh/authorized_keys >/dev/null <<EOF
command="/opt/openclaw/bin/vibe-forced-command.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ${pubkey}
EOF

sudo chmod 0600 /var/lib/openclaw-vibe/.ssh/authorized_keys
sudo chown openclaw-vibe:openclaw-vibe \
    /var/lib/openclaw-vibe/.ssh/authorized_keys

# Verify
sudo cat /var/lib/openclaw-vibe/.ssh/authorized_keys
# Expected: one line starting with:
#   command="/opt/openclaw/bin/vibe-forced-command.sh",no-port-... ssh-ed25519 AAAA...
```

> **Remove the decoy home directory** if one was accidentally created:
> ```bash
> sudo rm -rf /home/openclaw-vibe
> ```
> sshd will never look there for this user.

---

## 6 — Pin the VPS host key on the control plane

`StrictHostKeyChecking=yes` means sshd will refuse the connection unless the
host key is already in `known_hosts`.  Pin it explicitly:

```bash
# Run as root on the control plane
# Replace <VPS_HOST> with the hostname or IP used in OPENCLAW_SSH_HOST
sudo ssh-keyscan -H <VPS_HOST> | sudo tee -a /root/.ssh/known_hosts

# Verify the fingerprint matches what you expect (optional but recommended)
sudo ssh-keygen -lf /root/.ssh/known_hosts
```

---

## 7 — Configure the control plane

Add to `.env`:

```
OPENCLAW_SSH_HOST=openclaw-vibe@<VPS_HOST>
OPENCLAW_VIBE_WORKDIR=/srv/openclaw-vibe   # optional default workdir
```

Install the client shim from the repo:

```bash
# From the repo root, run as root on the control plane
sudo install -m 0755 bin/vibe-openclaw /usr/local/bin/vibe-openclaw
```

---

## 8 — Set up the READONLY lane (probes, terminal pills, autopilot)

The READONLY lane uses a separate system user, SSH key, and forced-command
wrapper (`bin/vibe-readonly-wrapper.sh`).  The wrapper allows only a finite
allowlist of read-only commands (e.g. `docker`, `find`, `grep`, `git`,
`sqlite3`, `uptime`).  No arbitrary passthrough, no shell expansion vectors.

### 8.1 — Create the `openclaw-readonly` system user on the VPS

```bash
sudo useradd \
    --system \
    --shell /bin/bash \
    --home-dir /var/lib/openclaw-readonly \
    --create-home \
    openclaw-readonly

# Verify
sudo getent passwd openclaw-readonly
```

### 8.2 — Install the readonly forced-command wrapper on the VPS

```bash
sudo install -d -m 0755 -o root -g root /opt/openclaw/bin

# Copy from repo root
sudo install -m 0755 -o root -g root \
    bin/vibe-readonly-wrapper.sh \
    /opt/openclaw/bin/vibe-readonly-wrapper.sh

# Verify
sudo ls -la /opt/openclaw/bin/vibe-readonly-wrapper.sh
```

### 8.3 — Generate a root-owned ED25519 key for the readonly lane

```bash
sudo ssh-keygen \
    -t ed25519 \
    -C "openclaw-readonly" \
    -f /root/.ssh/openclaw_readonly_ed25519 \
    -N ""

sudo ls -la /root/.ssh/openclaw_readonly_ed25519 /root/.ssh/openclaw_readonly_ed25519.pub
```

### 8.4 — Install the public key into `authorized_keys`

```bash
sudo install -d -m 0700 -o openclaw-readonly -g openclaw-readonly \
    /var/lib/openclaw-readonly/.ssh

pubkey="$(sudo cat /root/.ssh/openclaw_readonly_ed25519.pub)"

sudo tee /var/lib/openclaw-readonly/.ssh/authorized_keys >/dev/null <<EOF
command="/opt/openclaw/bin/vibe-readonly-wrapper.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ${pubkey}
EOF

sudo chmod 0600 /var/lib/openclaw-readonly/.ssh/authorized_keys
sudo chown openclaw-readonly:openclaw-readonly \
    /var/lib/openclaw-readonly/.ssh/authorized_keys

# Verify
sudo cat /var/lib/openclaw-readonly/.ssh/authorized_keys
# Expected: command="...vibe-readonly-wrapper.sh",... ssh-ed25519 AAAA...
```

### 8.5 — Pin the VPS host key (if not already done in step 6)

```bash
sudo ssh-keyscan -H <VPS_HOST> | sudo tee -a /root/.ssh/known_hosts
```

### 8.6 — Add to `.env`

```
OPENCLAW_SSH_READONLY_HOST=openclaw-readonly@<VPS_HOST>
```

**No fallback**: if this variable is left unset, all read-only features
(terminal pills, autopilot probes, snapshots, `/ops/report`) are disabled
and display a clear "not configured" message. They never silently fall back
to `OPENCLAW_SSH_HOST`.

### 8.7 — Smoke-test the READONLY lane

```bash
# Must succeed — runs an allowlisted command via the wrapper
sudo ssh \
    -i /root/.ssh/openclaw_readonly_ed25519 \
    -o IdentitiesOnly=yes \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=/root/.ssh/known_hosts \
    openclaw-readonly@<VPS_HOST> \
    'uptime'

# Must be rejected — blocklisted command
sudo ssh \
    -i /root/.ssh/openclaw_readonly_ed25519 \
    -o IdentitiesOnly=yes \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=/root/.ssh/known_hosts \
    openclaw-readonly@<VPS_HOST> \
    'rm -rf /tmp/test'
# Expected: "vibe-readonly-wrapper: error: command 'rm' is on the blocklist"
```

---

## 9 — Smoke-test (Vibe lane)

```bash
# Direct SSH test (must succeed before testing the shim)
sudo ssh \
    -i /root/.ssh/openclaw_vibe_ed25519 \
    -o IdentitiesOnly=yes \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=/root/.ssh/known_hosts \
    openclaw-vibe@<VPS_HOST> \
    'vibe --workdir /srv/openclaw-vibe --prompt ping'

# Shim test
OPENCLAW_SSH_HOST=openclaw-vibe@<VPS_HOST> \
sudo -E /usr/local/bin/vibe-openclaw \
    --workdir /srv/openclaw-vibe \
    --prompt "ping"
```

Expected output on success:
- `Authenticated to <host> using "publickey".` in verbose mode
- Remote: key options line referencing `/var/lib/openclaw-vibe/.ssh/authorized_keys`
- `vibe` output on stdout

---

## Troubleshooting

### Vibe lane (execution)

| Symptom | Likely cause | Fix |
|---|---|---|
| `Permission denied (publickey)` | Public key not in VPS `authorized_keys`, or wrong `.ssh` path | Confirm `sudo cat /var/lib/openclaw-vibe/.ssh/authorized_keys` contains the key |
| `Host key verification failed` | Host key not in `/root/.ssh/known_hosts` | Re-run `ssh-keyscan` (step 6) |
| `vibe not found or not executable` | No `/usr/local/bin/vibe` on VPS | Follow step 3 to install a system-visible `vibe` |
| `bad interpreter: Permission denied` (kernel) **or** `vibe interpreter '/home/…' lives under /home` (wrapper) | `vibe` was installed via `uv` under a user's `/home/...` tree; copying that entrypoint to `/usr/local/bin/vibe` preserves the unreadable shebang | Re-install per docs §3 Option A (root-owned venv) |
| `vibe interpreter '…' is not readable+executable as openclaw-vibe` (wrapper) | `vibe` shebang interpreter exists outside `/home/` but is not readable+executable for the system account | Fix permissions on the interpreter, or re-install per docs §3 Option A |
| `--workdir does not exist` | Forced-command rejected the path | Create the workdir on the VPS, or fix the path |
| `DENIED: command must start with 'vibe'` | Wrong command format sent | Check that `bin/vibe-openclaw` is up-to-date |
| `OPENCLAW_SSH_HOST is not set` | `.env` missing the variable | Set `OPENCLAW_SSH_HOST=openclaw-vibe@<host>` in `.env` |
| `connection timed out` | VPS unreachable or firewall | Check port 22 is open; verify `OPENCLAW_SSH_HOST` value |

### READONLY lane (probes / terminal pills)

| Symptom | Likely cause | Fix |
|---|---|---|
| `OPENCLAW_SSH_READONLY_HOST is not configured` in UI | `.env` missing the variable | Set `OPENCLAW_SSH_READONLY_HOST=openclaw-readonly@<host>` in `.env`; read-only features are disabled until set |
| `Permission denied (publickey)` on readonly lane | Readonly key not in `/var/lib/openclaw-readonly/.ssh/authorized_keys` | Follow step 8.4 to install the public key |
| `vibe-readonly-wrapper: error: command '…' is on the blocklist` | Command first token is blocked | Use an allowlisted command; mutations belong on the Vibe lane |
| `vibe-readonly-wrapper: error: command '…' is not in the read-only allowlist` | Command first token not in allowlist | Add it to `ALLOWED_FIRST_TOKENS` in `bin/vibe-readonly-wrapper.sh` if it is genuinely read-only |
| `vibe-readonly-wrapper: error: backtick sub-shell not permitted` | Command contains `` ` `` | Rewrite using `$()` alternative — actually `$()` is also blocked; simplify the command |
| `vibe-readonly-wrapper: error: \$() process substitution not permitted` | Command contains `$(…)` | Simplify command to remove process substitution |
| `vibe-readonly-wrapper: error: file redirect not permitted` | Command contains `>` or `<` | Only `2>&1` and `*/dev/null` redirects are allowed |
| Terminal pills show "not configured" | `OPENCLAW_SSH_READONLY_HOST` unset | Configure the READONLY lane (§8) or leave disabled — pills will not silently fall back to `OPENCLAW_SSH_HOST` |
