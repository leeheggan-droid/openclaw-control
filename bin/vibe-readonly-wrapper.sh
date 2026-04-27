#!/usr/bin/env bash
# bin/vibe-readonly-wrapper.sh — server-side forced-command wrapper for
# the openclaw-readonly SSH user/key.
#
# Deployed to: /opt/openclaw/bin/vibe-readonly-wrapper.sh
# Owner:       root:root  mode 0755
#
# This script is referenced in /var/lib/openclaw-readonly/.ssh/authorized_keys:
#   command="/opt/openclaw/bin/vibe-readonly-wrapper.sh",no-port-forwarding,\
#           no-X11-forwarding,no-agent-forwarding,no-pty <pubkey>
#
# Security invariants
# -------------------
#  1. Only the FIRST TOKEN of the command is checked; it must appear in
#     ALLOWED_FIRST_TOKENS.
#  2. Any command whose first token appears in BLOCKED_FIRST_TOKENS is
#     rejected, even if ALLOWED_FIRST_TOKENS also matched (blocklist wins).
#  3. Shell-injection vectors are rejected outright:
#       - backtick sub-shells  ( `...` )
#       - $( ) process substitution
#       - file redirects involving > or < (except 2>&1 and *>/dev/null)
#  4. The command runs via  bash -c "<cmd>"  with a hard 60-second timeout.
#  5. Interactive shell sessions are never permitted (SSH_ORIGINAL_COMMAND
#     must be non-empty).
#  6. No secrets are logged; only the first token and length are syslogged.
#
# To add a new allowed command, append its name to ALLOWED_FIRST_TOKENS.
# Do NOT add: bash, sh, eval, exec, or any write/mutate utilities.

set -euo pipefail

readonly PROG="vibe-readonly-wrapper"
readonly MAX_RUNTIME=60

# ── Allowed first tokens (read-only / inspection commands only) ───────────────
ALLOWED_FIRST_TOKENS=(
  docker find grep git sqlite3 uptime df free
  whoami id ls head tail cat wc awk sed cut
  sort uniq xargs echo printf stat who hostname
  pwd date
)

# ── Blocked first tokens (write/mutate/escalate — always rejected) ────────────
BLOCKED_FIRST_TOKENS=(
  rm mv cp chmod chown chgrp ln dd mkfs mount umount fdisk parted
  apt apt-get yum dnf pip npm uv pipx
  curl wget nc netcat ncat
  ssh scp rsync
  kill pkill killall
  systemctl service init telinit
  iptables ip6tables ufw nft firewalld
  tee
  bash sh dash zsh ksh
  eval source exec
  sudo su doas
  python python3 perl ruby node
)

# ── helpers ───────────────────────────────────────────────────────────────────

log()  { logger -t "$PROG" -- "$*"; }
die()  { log "DENIED: $*"; echo "${PROG}: error: $*" >&2; exit 1; }

# ── receive command from SSH_ORIGINAL_COMMAND ─────────────────────────────────

cmd="${SSH_ORIGINAL_COMMAND:-}"

log "invoked; first_token=$(echo "$cmd" | awk '{print $1}'); cmd_len=${#cmd}"

[[ -n "$cmd" ]] || die "interactive shell not permitted"

# ── shell-injection guard ─────────────────────────────────────────────────────

# Reject backtick sub-shells
if [[ "$cmd" == *'`'* ]]; then
    die "backtick sub-shell not permitted"
fi

# Reject $( process substitution
if [[ "$cmd" == *'$('* ]]; then
    die "\$() process substitution not permitted"
fi

# Reject file redirects (> < >> <<) except 2>&1 and /dev/null redirects.
# Strip known-safe patterns first, then check for remaining angle brackets.
_safe_stripped="${cmd//2>&1/}"
_safe_stripped="${_safe_stripped//>/dev/null/}"
_safe_stripped="${_safe_stripped//1>/dev/null/}"
_safe_stripped="${_safe_stripped//2>/dev/null/}"
if [[ "$_safe_stripped" == *'>'* || "$_safe_stripped" == *'<'* ]]; then
    die "file redirect not permitted (only 2>&1 and */dev/null are allowed)"
fi

# ── extract and validate the first token ─────────────────────────────────────

# shellcheck disable=SC2086
first_token="$(echo $cmd | awk '{print $1}')"

# Check blocklist first (blocklist wins over allowlist)
for blocked in "${BLOCKED_FIRST_TOKENS[@]}"; do
    if [[ "$first_token" == "$blocked" ]]; then
        die "command '${first_token}' is on the blocklist — mutation/escalation not permitted"
    fi
done

# Check allowlist
allowed=false
for ok in "${ALLOWED_FIRST_TOKENS[@]}"; do
    if [[ "$first_token" == "$ok" ]]; then
        allowed=true
        break
    fi
done

if [[ "$allowed" != "true" ]]; then
    die "command '${first_token}' is not in the read-only allowlist"
fi

# ── execute ───────────────────────────────────────────────────────────────────

log "ALLOWED: first_token=${first_token}"

if command -v timeout >/dev/null 2>&1; then
    exec timeout "$MAX_RUNTIME" bash -c "$cmd"
else
    exec bash -c "$cmd"
fi
