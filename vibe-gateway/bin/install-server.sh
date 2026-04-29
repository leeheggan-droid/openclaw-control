#!/usr/bin/env bash
# vibe-gateway/bin/install-server.sh — install the vibe SSH gateway on the VPS
#
# Run as root on the target Ubuntu 24.04 server.
#
# What this script does:
#   1. Creates the system user 'openclaw-vibe' with home /var/lib/openclaw-vibe
#   2. Creates /opt/openclaw/gateway/ and installs scripts + config
#   3. Creates /var/lib/openclaw-vibe/  (persistent vibe cache)
#   4. Sets up /var/lib/openclaw-vibe/.ssh/authorized_keys
#   5. Prompts the operator to paste the agent's public key
#   6. Prints the exact authorized_keys line to add
#
# Usage (run from repo root):
#   sudo bash vibe-gateway/bin/install-server.sh [--pubkey-file /path/to/key.pub]

set -euo pipefail

readonly PROG="install-server"
readonly INSTALL_DIR="/opt/openclaw/gateway"
readonly VIBE_USER="openclaw-vibe"
readonly VIBE_HOME="/var/lib/openclaw-vibe"
readonly CACHE_DIR="/var/lib/openclaw-vibe/cache"

die()  { printf '%s: error: %s\n' "$PROG" "$*" >&2; exit 1; }
info() { printf '[%s] %s\n' "$PROG" "$*"; }

# ── must run as root ──────────────────────────────────────────────────────────

[[ "$(id -u)" -eq 0 ]] || die "must run as root"

# ── determine repo root (script is in vibe-gateway/bin/) ──────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
GATEWAY_SRC="${REPO_ROOT}/vibe-gateway"

[[ -d "$GATEWAY_SRC" ]] || die "gateway source directory not found: ${GATEWAY_SRC}"

# ── parse arguments ───────────────────────────────────────────────────────────

pubkey_file=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --pubkey-file)
            shift
            [[ $# -gt 0 ]] || die "--pubkey-file requires a path"
            pubkey_file="$1"
            shift
            ;;
        --pubkey-file=*)
            pubkey_file="${1#*=}"
            shift
            ;;
        -h|--help)
            cat <<EOF
Usage: sudo bash vibe-gateway/bin/install-server.sh [--pubkey-file /path/to/key.pub]

  --pubkey-file   Path to the agent's SSH public key file.
                  If omitted, you will be prompted to paste the key.
EOF
            exit 0
            ;;
        *) die "unknown argument: $1" ;;
    esac
done

# ── 1. create system user ─────────────────────────────────────────────────────

info "creating system user '${VIBE_USER}'"
if id "$VIBE_USER" &>/dev/null; then
    info "user '${VIBE_USER}' already exists — skipping useradd"
else
    useradd \
        --system \
        --shell /usr/sbin/nologin \
        --home-dir "$VIBE_HOME" \
        --create-home \
        "$VIBE_USER"
    info "user '${VIBE_USER}' created"
fi

# ── 2. create install directory and copy scripts ──────────────────────────────

info "installing gateway scripts to ${INSTALL_DIR}"
install -d -m 0755 -o root -g root "${INSTALL_DIR}/bin"
install -d -m 0755 -o root -g root "${INSTALL_DIR}/config"

install -m 0755 -o root -g root \
    "${GATEWAY_SRC}/bin/forced-command.sh" \
    "${INSTALL_DIR}/bin/forced-command.sh"

install -m 0755 -o root -g root \
    "${GATEWAY_SRC}/bin/run-vibe-container.sh" \
    "${INSTALL_DIR}/bin/run-vibe-container.sh"

# Install allowlist only if not already present (preserve operator edits).
if [[ ! -f "${INSTALL_DIR}/config/allowed-workdirs.txt" ]]; then
    install -m 0644 -o root -g root \
        "${GATEWAY_SRC}/config/allowed-workdirs.txt" \
        "${INSTALL_DIR}/config/allowed-workdirs.txt"
    info "installed default allowlist — edit ${INSTALL_DIR}/config/allowed-workdirs.txt"
else
    info "allowlist already present — skipping (not overwritten)"
fi

# ── 3. create cache/home directories ─────────────────────────────────────────

info "creating persistent cache dir ${CACHE_DIR}"
install -d -m 0755 -o "$VIBE_USER" -g "$VIBE_USER" "$CACHE_DIR"

# ── 4. set up .ssh directory ──────────────────────────────────────────────────

info "setting up ${VIBE_HOME}/.ssh"
install -d -m 0700 -o "$VIBE_USER" -g "$VIBE_USER" "${VIBE_HOME}/.ssh"

auth_keys="${VIBE_HOME}/.ssh/authorized_keys"
if [[ ! -f "$auth_keys" ]]; then
    install -m 0600 -o "$VIBE_USER" -g "$VIBE_USER" /dev/null "$auth_keys"
fi

# ── 5. add public key ─────────────────────────────────────────────────────────

forced_cmd="command=\"${INSTALL_DIR}/bin/forced-command.sh\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding"

if [[ -n "$pubkey_file" ]]; then
    [[ -f "$pubkey_file" ]] || die "public key file not found: ${pubkey_file}"
    pubkey="$(cat "$pubkey_file")"
else
    info ""
    info "Paste the agent's SSH public key below, then press Enter followed by Ctrl-D:"
    pubkey="$(cat)"
fi

pubkey="$(printf '%s' "$pubkey" | tr -d '\r\n')"
[[ -n "$pubkey" ]] || die "public key is empty"

authkeys_line="${forced_cmd} ${pubkey}"

# Append only if not already present.
if grep -qF "$pubkey" "$auth_keys" 2>/dev/null; then
    info "public key already present in authorized_keys — skipping"
else
    printf '%s\n' "$authkeys_line" >> "$auth_keys"
    chown "${VIBE_USER}:${VIBE_USER}" "$auth_keys"
    chmod 600 "$auth_keys"
    info "public key added to ${auth_keys}"
fi

# ── 6. generate outbound SSH identity for vibe's SSH actions ─────────────────
#
# When vibe runs as openclaw-vibe and needs to reach openclaw-readonly@localhost
# (e.g. for uptime, ls, git, docker checks), it needs a private key that is
# authorised for that user.  This step:
#   a. Generates an ed25519 key pair for openclaw-vibe (outbound use only).
#   b. Writes ~/.ssh/config to point SSH at that key for localhost connections.
#   c. Pre-seeds ~/.ssh/known_hosts so BatchMode=yes never blocks on a TOFU prompt.
#   d. Ensures openclaw-readonly exists, has its wrapper installed, and accepts
#      the new key (with the forced-command restriction).

readonly VIBE_OUTBOUND_KEY="${VIBE_HOME}/.ssh/openclaw_vibe_outbound_ed25519"
readonly VIBE_SSH_CONFIG="${VIBE_HOME}/.ssh/config"
readonly READONLY_USER="openclaw-readonly"
readonly READONLY_HOME="/var/lib/openclaw-readonly"
readonly READONLY_WRAPPER="/opt/openclaw/bin/vibe-readonly-wrapper.sh"

# 6a — generate outbound key
info "generating outbound SSH key for ${VIBE_USER} (for vibe SSH actions)"
if [[ -f "${VIBE_OUTBOUND_KEY}" ]]; then
    info "outbound key already exists — skipping keygen"
else
    ssh-keygen \
        -t ed25519 \
        -C "openclaw-vibe-outbound" \
        -f "${VIBE_OUTBOUND_KEY}" \
        -N ""
    chown "${VIBE_USER}:${VIBE_USER}" "${VIBE_OUTBOUND_KEY}" "${VIBE_OUTBOUND_KEY}.pub"
    chmod 0600 "${VIBE_OUTBOUND_KEY}"
    chmod 0644 "${VIBE_OUTBOUND_KEY}.pub"
    info "outbound key generated: ${VIBE_OUTBOUND_KEY}"
fi

# 6b — write ~/.ssh/config for openclaw-vibe
if [[ ! -f "${VIBE_SSH_CONFIG}" ]]; then
    cat > "${VIBE_SSH_CONFIG}" <<EOF
# Written by install-server.sh — SSH client config for openclaw-vibe outbound connections.
Host localhost 127.0.0.1
    IdentityFile ${VIBE_OUTBOUND_KEY}
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking yes
    UserKnownHostsFile ${VIBE_HOME}/.ssh/known_hosts
EOF
    chown "${VIBE_USER}:${VIBE_USER}" "${VIBE_SSH_CONFIG}"
    chmod 0600 "${VIBE_SSH_CONFIG}"
    info "wrote SSH client config: ${VIBE_SSH_CONFIG}"
else
    info "SSH client config already present — skipping (not overwritten)"
fi

# 6c — pre-seed known_hosts for localhost
info "pre-seeding ${VIBE_HOME}/.ssh/known_hosts for localhost"
known_hosts_file="${VIBE_HOME}/.ssh/known_hosts"
[[ -f "$known_hosts_file" ]] || install -m 0644 -o "$VIBE_USER" -g "$VIBE_USER" /dev/null "$known_hosts_file"
ssh-keyscan -H 127.0.0.1 2>/dev/null >> "$known_hosts_file" || info "ssh-keyscan 127.0.0.1 failed — sshd may not be running yet; re-run install-server.sh after sshd starts"
ssh-keyscan -H localhost  2>/dev/null >> "$known_hosts_file" || info "ssh-keyscan localhost failed — sshd may not be running yet; re-run install-server.sh after sshd starts"
chown "${VIBE_USER}:${VIBE_USER}" "$known_hosts_file"
info "known_hosts updated"

# 6d — ensure openclaw-readonly user exists
info "ensuring system user '${READONLY_USER}' exists"
if id "$READONLY_USER" &>/dev/null; then
    info "user '${READONLY_USER}' already exists"
else
    useradd \
        --system \
        --shell /usr/sbin/nologin \
        --home-dir "$READONLY_HOME" \
        --create-home \
        "$READONLY_USER"
    info "user '${READONLY_USER}' created"
fi

# 6e — install readonly forced-command wrapper (if not already present)
install -d -m 0755 -o root -g root /opt/openclaw/bin
readonly_wrapper_src="${REPO_ROOT}/bin/vibe-readonly-wrapper.sh"
if [[ ! -x "$READONLY_WRAPPER" ]]; then
    if [[ -f "$readonly_wrapper_src" ]]; then
        install -m 0755 -o root -g root "$readonly_wrapper_src" "$READONLY_WRAPPER"
        info "installed readonly wrapper: ${READONLY_WRAPPER}"
    else
        info "warning: ${readonly_wrapper_src} not found — skipping wrapper install"
        info "         Copy bin/vibe-readonly-wrapper.sh to ${READONLY_WRAPPER} manually"
    fi
else
    info "readonly wrapper already installed: ${READONLY_WRAPPER}"
fi

# 6f — add outbound key to openclaw-readonly authorized_keys
info "authorising openclaw-vibe outbound key for ${READONLY_USER}"
readonly_ssh_dir="${READONLY_HOME}/.ssh"
readonly_auth_keys="${readonly_ssh_dir}/authorized_keys"
install -d -m 0700 -o "$READONLY_USER" -g "$READONLY_USER" "$readonly_ssh_dir"
[[ -f "$readonly_auth_keys" ]] || \
    install -m 0600 -o "$READONLY_USER" -g "$READONLY_USER" /dev/null "$readonly_auth_keys"

vibe_outbound_pubkey="$(cat "${VIBE_OUTBOUND_KEY}.pub")"
readonly_forced_cmd="command=\"${READONLY_WRAPPER}\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding"
if grep -qF "$vibe_outbound_pubkey" "$readonly_auth_keys" 2>/dev/null; then
    info "outbound key already present in ${readonly_auth_keys}"
else
    printf '%s %s\n' "$readonly_forced_cmd" "$vibe_outbound_pubkey" >> "$readonly_auth_keys"
    chown "${READONLY_USER}:${READONLY_USER}" "$readonly_auth_keys"
    chmod 0600 "$readonly_auth_keys"
    info "outbound key added to ${readonly_auth_keys}"
fi

# ── 8. install systemd unit (optional) ───────────────────────────────────────

systemd_unit="${GATEWAY_SRC}/systemd/openclaw-vibe-gateway.service"
if [[ -f "$systemd_unit" ]]; then
    install -m 0644 -o root -g root \
        "$systemd_unit" \
        /etc/systemd/system/openclaw-vibe-gateway.service
    systemctl daemon-reload
    info "systemd unit installed — enable with: systemctl enable --now openclaw-vibe-gateway"
fi

# ── 9. summary ────────────────────────────────────────────────────────────────

info ""
info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
info "Installation complete."
info ""
info "Next steps:"
info "  1. Edit the allowlist:  ${INSTALL_DIR}/config/allowed-workdirs.txt"
info "  2. Build the Docker image (from repo root):"
info "       docker build -t openclaw-vibe-gateway:latest vibe-gateway/docker/"
info "  3. Smoke-test vibe SSH actions (from the VPS, as root):"
info "       sudo -u ${VIBE_USER} ssh -o BatchMode=yes -o StrictHostKeyChecking=yes \\"
info "           ${READONLY_USER}@127.0.0.1 uptime"
info "  4. Smoke-test the gateway (from the control plane):"
info "       PROMPT='echo hello'"
info "       PROMPT_B64=\$(printf '%s' \"\$PROMPT\" | base64 -w0)"
info "       ssh -i ~/.ssh/openclaw_vibe_ed25519 \\"
info "           -o BatchMode=yes -o StrictHostKeyChecking=yes \\"
info "           ${VIBE_USER}@<HOST> \\"
info "           \"OPENCLAW_PROMPT_B64=\$PROMPT_B64 vibe --workdir /srv/openclaw-work -p __B64__\""
info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
