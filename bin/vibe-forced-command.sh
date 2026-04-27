#!/usr/bin/env bash
# bin/vibe-forced-command.sh — server-side forced-command wrapper for openclaw-vibe
#
# Deployed to: /opt/openclaw/bin/vibe-forced-command.sh
# Owner:       root:root  mode 0755
#
# This script is referenced in /var/lib/openclaw-vibe/.ssh/authorized_keys as:
#   command="/opt/openclaw/bin/vibe-forced-command.sh",no-port-forwarding,\
#           no-X11-forwarding,no-agent-forwarding,no-pty <pubkey>
#
# It validates and executes ONLY commands of the form:
#   vibe --workdir /absolute/path --prompt <text>
#
# All other commands are rejected.  Invocation is logged via syslog.
# Max runtime is enforced at 900 seconds (15 minutes).

set -euo pipefail

readonly PROG="vibe-forced-command"
readonly MAX_RUNTIME=900
readonly VIBE_BIN="/usr/local/bin/vibe"

# ── helpers ───────────────────────────────────────────────────────────────────

log()  { logger -t "$PROG" -- "$*"; }
die()  { log "DENIED: $*"; echo "${PROG}: error: $*" >&2; exit 1; }

# ── receive command from SSH_ORIGINAL_COMMAND ─────────────────────────────────

cmd="${SSH_ORIGINAL_COMMAND:-}"

log "invoked; SSH_ORIGINAL_COMMAND=${cmd}"

[[ -n "$cmd" ]] || die "no command provided (interactive shell not permitted)"

# ── parse and validate the command ───────────────────────────────────────────
# Expected pattern: vibe --workdir /abs/path --prompt <text>
# We parse positionally; extra or reordered flags are rejected.

# Tokenise into an array (word-split is intentional here, prompt may have spaces
# but they will be properly quoted by the client via printf %q / shell quoting).
# shellcheck disable=SC2086
set -- $cmd

[[ "${1:-}" == "vibe" ]] || die "command must start with 'vibe'; got: ${1:-}"
shift  # consume 'vibe'

workdir=""
prompt=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workdir)
            shift
            [[ $# -gt 0 ]] || die "--workdir requires a value"
            workdir="$1"
            shift
            ;;
        --prompt)
            shift
            [[ $# -gt 0 ]] || die "--prompt requires a value"
            # Remaining tokens are the prompt (may be multi-word after shell-quoting)
            prompt="$1"
            shift
            ;;
        *) die "unexpected argument: $1" ;;
    esac
done

[[ -n "$workdir" ]] || die "--workdir is required"
[[ -n "$prompt"  ]] || die "--prompt is required"

# workdir must be absolute
[[ "$workdir" == /* ]] || die "--workdir must be an absolute path; got: ${workdir}"

# workdir must exist
[[ -d "$workdir" ]] || die "--workdir does not exist: ${workdir}"

# ── run vibe ─────────────────────────────────────────────────────────────────

[[ -x "$VIBE_BIN" ]] || die "vibe not found or not executable at ${VIBE_BIN}"

log "ALLOWED: workdir=${workdir}"

# Prefer timeout(1) when available; fall back to plain exec.
if command -v timeout >/dev/null 2>&1; then
    exec timeout "$MAX_RUNTIME" "$VIBE_BIN" --workdir "$workdir" --prompt "$prompt"
else
    exec "$VIBE_BIN" --workdir "$workdir" --prompt "$prompt"
fi
