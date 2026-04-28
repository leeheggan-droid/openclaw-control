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

# ── 6. install systemd unit (optional) ───────────────────────────────────────

systemd_unit="${GATEWAY_SRC}/systemd/openclaw-vibe-gateway.service"
if [[ -f "$systemd_unit" ]]; then
    install -m 0644 -o root -g root \
        "$systemd_unit" \
        /etc/systemd/system/openclaw-vibe-gateway.service
    systemctl daemon-reload
    info "systemd unit installed — enable with: systemctl enable --now openclaw-vibe-gateway"
fi

# ── 7. summary ────────────────────────────────────────────────────────────────

info ""
info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
info "Installation complete."
info ""
info "Next steps:"
info "  1. Edit the allowlist:  ${INSTALL_DIR}/config/allowed-workdirs.txt"
info "  2. Build the Docker image (from repo root):"
info "       docker build -t openclaw-vibe-gateway:latest vibe-gateway/docker/"
info "  3. Smoke-test (from the control plane):"
info "       PROMPT='echo hello'"
info "       PROMPT_B64=\$(printf '%s' \"\$PROMPT\" | base64 -w0)"
info "       ssh -i ~/.ssh/openclaw_vibe_ed25519 \\"
info "           -o BatchMode=yes -o StrictHostKeyChecking=yes \\"
info "           ${VIBE_USER}@<HOST> \\"
info "           \"OPENCLAW_PROMPT_B64=\$PROMPT_B64 vibe --workdir /srv/openclaw-work -p __B64__\""
info "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
