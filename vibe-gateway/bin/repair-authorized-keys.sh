#!/usr/bin/env bash
# vibe-gateway/bin/repair-authorized-keys.sh — repair authorized_keys for the
# openclaw-vibe and/or openclaw-readonly SSH gateway users.
#
# Run as root on the VPS.
#
# Problems this script fixes:
#   1. Unrestricted key — the public key was added without a forced-command,
#      allowing the agent to open an unrestricted interactive shell.
#   2. Wrong forced-command path — e.g. the legacy path
#      /opt/openclaw/bin/vibe-forced-command.sh was used instead of the current
#      /opt/openclaw/gateway/bin/forced-command.sh.
#   3. Missing key options — no-pty / no-port-forwarding restrictions absent.
#
# For each selected lane the script:
#   a. Reads existing keys from authorized_keys (stripping any stale options).
#   b. Rewrites the file with the correct forced-command options.
#   c. Restores ownership and permissions.
#   d. Prints a diff-style summary of what changed.
#
# The rewrite is atomic: the new content is written to a temporary file and
# then moved into place so sshd never reads a half-written file.
#
# Usage (run from repo root or anywhere as root):
#   sudo bash vibe-gateway/bin/repair-authorized-keys.sh [options]
#
# Options:
#   --lane vibe|readonly|both   Lane(s) to repair (default: both)
#   --pubkey-file /path/to.pub  Path to a .pub file to use instead of the
#                               key(s) already in authorized_keys.  Use this
#                               when authorized_keys is missing or empty.
#   -h, --help                  Show this help and exit

set -euo pipefail

readonly PROG="repair-authorized-keys"

# ── canonical paths ────────────────────────────────────────────────────────────

# Vibe lane
readonly VIBE_USER="openclaw-vibe"
readonly VIBE_HOME="/var/lib/openclaw-vibe"
readonly VIBE_AUTH_KEYS="${VIBE_HOME}/.ssh/authorized_keys"
readonly VIBE_FORCED_CMD="/opt/openclaw/gateway/bin/forced-command.sh"
readonly VIBE_KEY_OPTIONS="command=\"${VIBE_FORCED_CMD}\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding"

# Readonly lane
readonly READONLY_USER="openclaw-readonly"
readonly READONLY_HOME="/var/lib/openclaw-readonly"
readonly READONLY_AUTH_KEYS="${READONLY_HOME}/.ssh/authorized_keys"
readonly READONLY_FORCED_CMD="/opt/openclaw/bin/vibe-readonly-wrapper.sh"
readonly READONLY_KEY_OPTIONS="command=\"${READONLY_FORCED_CMD}\",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding"

# ── helpers ────────────────────────────────────────────────────────────────────

die()  { printf '%s: error: %s\n' "$PROG" "$*" >&2; exit 1; }
info() { printf '[%s] %s\n'       "$PROG" "$*"; }
warn() { printf '[%s] WARNING: %s\n' "$PROG" "$*" >&2; }

# strip_options <line>
# Given an authorized_keys line (possibly starting with options), echo just the
# raw key material: "<type> <base64> [comment]".
# Lines that are blank or start with '#' are passed through unchanged.
# Returns exit code 1 if the line cannot be parsed as a key.
strip_options() {
    local line="$1"

    # Pass through blank lines and comments.
    if [[ -z "$line" || "$line" == \#* ]]; then
        printf '%s\n' "$line"
        return 0
    fi

    # If the first token is already a key type, no options to strip.
    # Known key types (RFC 4253 + OpenSSH extensions):
    #   ssh-rsa  ssh-dss  ssh-ed25519  ssh-ed448
    #   ecdsa-sha2-nistp256  ecdsa-sha2-nistp384  ecdsa-sha2-nistp521
    #   sk-ssh-ed25519@openssh.com  sk-ecdsa-sha2-nistp256@openssh.com
    if [[ "$line" =~ ^(ssh-|ecdsa-|sk-) ]]; then
        printf '%s\n' "$line"
        return 0
    fi

    # The line has options before the key.  Find the first token that looks
    # like a key type and return everything from that token onwards.
    # We scan token by token; options may contain quoted strings with spaces
    # (e.g. command="...some path...") so we use a simple state machine.
    local rest="$line"
    local in_quote=0
    local i

    for (( i=0; i<${#rest}; i++ )); do
        local ch="${rest:$i:1}"
        if [[ $in_quote -eq 1 ]]; then
            [[ "$ch" == '"' ]] && in_quote=0
            continue
        fi
        if [[ "$ch" == '"' ]]; then
            in_quote=1
            continue
        fi
        if [[ "$ch" == ' ' ]]; then
            # End of a token — check if the remainder starts with a key type.
            local tail="${rest:$(( i+1 ))}"
            if [[ "$tail" =~ ^(ssh-|ecdsa-|sk-) ]]; then
                printf '%s\n' "$tail"
                return 0
            fi
        fi
    done

    warn "could not parse key from line: ${line:0:60}…"
    return 1
}

# ── must run as root ────────────────────────────────────────────────────────────

[[ "$(id -u)" -eq 0 ]] || die "must run as root"

# ── parse arguments ────────────────────────────────────────────────────────────

lane="both"
pubkey_file=""
fix_uid=0
fix_ssh_config=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --lane)
            shift
            [[ $# -gt 0 ]] || die "--lane requires a value (vibe|readonly|both)"
            lane="$1"
            shift
            ;;
        --lane=*)
            lane="${1#*=}"
            shift
            ;;
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
        --fix-uid)
            fix_uid=1
            shift
            ;;
        --fix-ssh-config)
            fix_ssh_config=1
            shift
            ;;
        -h|--help)
            cat <<EOF
Usage: sudo bash vibe-gateway/bin/repair-authorized-keys.sh [options]

Repairs the authorized_keys file for openclaw SSH gateway users by ensuring
every key has the correct forced-command options and no unrestricted entries.

Options:
  --lane vibe|readonly|both   Which lane to repair (default: both)
  --pubkey-file /path/to.pub  Path to a .pub file to seed the key when
                              authorized_keys is missing or empty
  --fix-uid                   Change openclaw-vibe UID to 1500 and re-chown its
                              home directory.  Required on systems where the user
                              was created as a system account (uid<1000) before
                              the uid=1500 requirement was introduced.  This
                              ensures the Docker container (vibeuser uid=1500)
                              can read the 0600 SSH key files.
  --fix-ssh-config            Rewrite /var/lib/openclaw-vibe/.ssh/config to use
                              ~ -relative paths (IdentityFile, UserKnownHostsFile)
                              so the same file works both on the host and inside
                              the Docker container where ~/.ssh is bind-mounted.
  -h, --help                  Show this help

Vibe lane
  User:          ${VIBE_USER}
  auth keys:     ${VIBE_AUTH_KEYS}
  forced-command ${VIBE_FORCED_CMD}

Readonly lane
  User:          ${READONLY_USER}
  auth keys:     ${READONLY_AUTH_KEYS}
  forced-command ${READONLY_FORCED_CMD}
EOF
            exit 0
            ;;
        *) die "unknown argument: $1" ;;
    esac
done

case "$lane" in
    vibe|readonly|both) ;;
    *) die "--lane must be 'vibe', 'readonly', or 'both'; got '${lane}'" ;;
esac

# ── optional: fix openclaw-vibe UID ───────────────────────────────────────────
# Deployments where openclaw-vibe was created with --system receive a UID below
# 1000.  The Docker container runs as vibeuser (UID 1500) and cannot read 0600
# key files owned by a different UID.  --fix-uid changes the host UID to 1500
# and re-chowns the home directory so ownership matches the container.

if [[ $fix_uid -eq 1 ]]; then
    current_uid="$(id -u "$VIBE_USER" 2>/dev/null)" \
        || die "user '${VIBE_USER}' does not exist"
    if [[ "$current_uid" -eq 1500 ]]; then
        info "openclaw-vibe already has uid=1500 — skipping --fix-uid"
    else
        info "changing ${VIBE_USER} uid from ${current_uid} to 1500"
        usermod --uid 1500 "$VIBE_USER"
        groupmod --gid 1500 "$VIBE_USER" 2>/dev/null || true
        find "$VIBE_HOME" -user "$current_uid" -exec chown -h "${VIBE_USER}:${VIBE_USER}" {} + 2>/dev/null || true
        chown -R "${VIBE_USER}:${VIBE_USER}" "$VIBE_HOME"
        info "✓ ${VIBE_USER} uid changed to 1500; ${VIBE_HOME} re-chowned"
    fi
fi

# ── optional: rewrite SSH config to use ~-relative paths ─────────────────────
# The SSH config written by older installs uses absolute paths like
# /var/lib/openclaw-vibe/.ssh/... which do not resolve inside the Docker
# container (home=/home/vibeuser).  --fix-ssh-config rewrites the file to use
# ~ -relative paths that work in both environments.

if [[ $fix_ssh_config -eq 1 ]]; then
    ssh_config="${VIBE_HOME}/.ssh/config"
    if [[ ! -f "$ssh_config" ]]; then
        warn "SSH config not found: ${ssh_config} — nothing to fix"
    else
        # Check whether absolute paths are present.
        if grep -qE 'IdentityFile[[:space:]]*/|UserKnownHostsFile[[:space:]]*/'\
                "$ssh_config" 2>/dev/null; then
            info "rewriting absolute paths to ~ -relative in ${ssh_config}"
            tmp="$(mktemp "${ssh_config}.tmp.XXXXXX")"
            chmod 0600 "$tmp"
            sed \
                -e 's|^\([[:space:]]*IdentityFile[[:space:]]*\).*/\.ssh/\(.*\)|\1~/.ssh/\2|' \
                -e 's|^\([[:space:]]*UserKnownHostsFile[[:space:]]*\).*/\.ssh/\(.*\)|\1~/.ssh/\2|' \
                "$ssh_config" > "$tmp"
            mv "$tmp" "$ssh_config"
            chown "${VIBE_USER}:${VIBE_USER}" "$ssh_config"
            chmod 0600 "$ssh_config"
            info "✓ SSH config updated"
            info "New content:"
            sed 's/^/  /' "$ssh_config" >&2
        else
            info "SSH config already uses relative paths — no changes needed"
        fi
    fi
fi

# ── optional: validate pubkey file ────────────────────────────────────────────

if [[ -n "$pubkey_file" ]]; then
    [[ -f "$pubkey_file" ]] || die "pubkey file not found: ${pubkey_file}"
fi

# ── repair function ────────────────────────────────────────────────────────────

repair_lane() {
    local lane_name="$1"         # "vibe" or "readonly"
    local sys_user="$2"
    local auth_keys="$3"
    local key_options="$4"
    local forced_cmd_path="$5"

    info ""
    info "━━  Repairing ${lane_name} lane (${sys_user})  ━━"

    # ── verify forced-command exists ──────────────────────────────────────────
    if [[ ! -x "$forced_cmd_path" ]]; then
        warn "forced-command not found or not executable: ${forced_cmd_path}"
        warn "Install it before repairing, or the key will still be non-functional."
    fi

    # ── ensure .ssh directory ─────────────────────────────────────────────────
    local ssh_dir
    ssh_dir="$(dirname "$auth_keys")"
    if [[ ! -d "$ssh_dir" ]]; then
        install -d -m 0700 -o "$sys_user" -g "$sys_user" "$ssh_dir"
        info "created ${ssh_dir}"
    fi

    # ── collect raw key lines ─────────────────────────────────────────────────
    # Source: existing authorized_keys file, or --pubkey-file, or both.
    declare -a raw_keys=()

    if [[ -n "$pubkey_file" ]]; then
        local pk
        pk="$(tr -d '\r\n' < "$pubkey_file")"
        [[ -n "$pk" ]] || die "pubkey file is empty: ${pubkey_file}"
        raw_keys+=( "$pk" )
        info "using pubkey from ${pubkey_file}"
    fi

    if [[ -f "$auth_keys" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            # Skip blank lines and comments.
            [[ -z "$line" || "$line" == \#* ]] && continue

            # Strip any existing options.
            local raw
            raw="$(strip_options "$line")" || { warn "skipping unparseable line"; continue; }

            # Avoid duplicates when --pubkey-file already added this key.
            local already=0
            for existing in "${raw_keys[@]:-}"; do
                if [[ "$existing" == "$raw" ]]; then
                    already=1
                    break
                fi
            done
            [[ $already -eq 1 ]] && continue

            raw_keys+=( "$raw" )
        done < "$auth_keys"
    fi

    if [[ ${#raw_keys[@]} -eq 0 ]]; then
        warn "no keys found for ${lane_name} lane — nothing to write."
        warn "Run again with --pubkey-file to seed the key."
        return 0
    fi

    # ── build new file content ────────────────────────────────────────────────
    local tmp_file
    tmp_file="$(mktemp "${auth_keys}.tmp.XXXXXX")"
    # Ensure tmp file has restrictive perms from the start.
    chmod 0600 "$tmp_file"

    for raw in "${raw_keys[@]}"; do
        printf '%s %s\n' "$key_options" "$raw" >> "$tmp_file"
    done

    # ── compare and apply ─────────────────────────────────────────────────────
    if [[ -f "$auth_keys" ]] && cmp -s "$tmp_file" "$auth_keys"; then
        info "authorized_keys already correct — no changes needed."
        rm -f "$tmp_file"
        return 0
    fi

    # Show what is changing.
    if [[ -f "$auth_keys" ]]; then
        info "Current ${auth_keys}:"
        sed 's/^/  < /' "$auth_keys" >&2
        info "Replacement:"
    else
        info "Creating ${auth_keys}:"
    fi
    sed 's/^/  > /' "$tmp_file" >&2

    # Atomic replace.
    mv "$tmp_file" "$auth_keys"
    chown "${sys_user}:${sys_user}" "$auth_keys"
    chmod 0600 "$auth_keys"

    info "✓ ${auth_keys} repaired (${#raw_keys[@]} key(s) written)."
}

# ── run selected lane(s) ───────────────────────────────────────────────────────

if [[ "$lane" == "vibe" || "$lane" == "both" ]]; then
    repair_lane "vibe" \
        "$VIBE_USER" \
        "$VIBE_AUTH_KEYS" \
        "$VIBE_KEY_OPTIONS" \
        "$VIBE_FORCED_CMD"
fi

if [[ "$lane" == "readonly" || "$lane" == "both" ]]; then
    repair_lane "readonly" \
        "$READONLY_USER" \
        "$READONLY_AUTH_KEYS" \
        "$READONLY_KEY_OPTIONS" \
        "$READONLY_FORCED_CMD"
fi

info ""
info "Repair complete."
info ""
info "Next: smoke-test each repaired lane (see docs/ssh-execution-gateway.md §9 / §8.7)."
