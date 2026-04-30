#!/usr/bin/env bash
# vibe-gateway/bin/forced-command.sh — server-side forced-command wrapper
#
# Installed to: /opt/openclaw/gateway/bin/forced-command.sh
# Owner:        root:root  mode 0755
#
# Referenced from /var/lib/openclaw-vibe/.ssh/authorized_keys:
#   command="/opt/openclaw/gateway/bin/forced-command.sh",\
#   no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding \
#   ssh-ed25519 AAAA… openclaw-vibe-agent
#
# Accepts ONLY a command of the exact shape:
#   vibe --workdir /absolute/path -p __B64__
#   vibe --workdir /absolute/path --prompt __B64__
#
# The actual prompt text is read from the environment variable
# OPENCLAW_PROMPT_B64 (base64-encoded).  Using a sentinel token (__B64__) in
# the command line and reading the real value from an env var prevents shell
# quoting bugs when the prompt contains special characters.
#
# Security guarantees:
#   • command shape is validated before any work is done
#   • workdir is checked against /opt/openclaw/gateway/config/allowed-workdirs.txt
#   • workdir must be an absolute path that exists on the host
#   • prompt is never eval'd; it is passed to the container via env var
#   • a file lock (/run/lock/openclaw-vibe.lock) prevents concurrent runs
#   • lock acquisition and run are bounded by MAX_RUNTIME seconds
#   • all events are logged via syslog with tag "openclaw-vibe-forced"

set -euo pipefail

# ── constants ─────────────────────────────────────────────────────────────────

readonly PROG="openclaw-vibe-forced"
readonly INSTALL_DIR="/opt/openclaw/gateway"
readonly ALLOWLIST="${INSTALL_DIR}/config/allowed-workdirs.txt"
readonly RUNNER="${INSTALL_DIR}/bin/run-vibe-container.sh"
readonly LOCK_FILE="/run/lock/openclaw-vibe.lock"
readonly MAX_RUNTIME=900   # seconds (15 min)
readonly LOCK_TIMEOUT=10   # seconds to wait for lock before giving up

# ── helpers ───────────────────────────────────────────────────────────────────

log() { logger -t "$PROG" -- "$*"; }
die() { log "DENIED: $*"; printf '%s: error: %s\n' "$PROG" "$*" >&2; exit 1; }

# ── receive command ───────────────────────────────────────────────────────────

cmd="${SSH_ORIGINAL_COMMAND:-}"
log "invoked; user=$(id -un) cmd=${cmd}"

[[ -n "$cmd" ]] || die "no command provided (interactive shell not permitted)"

# ── validate command shape ────────────────────────────────────────────────────
# Expected (order-fixed):
#   vibe --workdir /abs/path --prompt __B64__
#   vibe --workdir /abs/path -p __B64__
#
# Callers may also prefix the command with an inline env-var assignment:
#   OPENCLAW_PROMPT_B64=<b64> vibe --workdir /abs/path -p __B64__
#
# When SSH forwards the command as a single string (the most common usage),
# sshd places the entire string — including any NAME=VALUE prefix — into
# SSH_ORIGINAL_COMMAND verbatim.  It does NOT parse shell env-var assignments.
# We handle this explicitly: only OPENCLAW_PROMPT_B64 is accepted as an inline
# assignment; any other NAME=VALUE token at position 0 is rejected.
#
# We parse tokens manually — no eval, no bash -c.

read -r -a tokens <<< "$cmd"

# Strip leading OPENCLAW_PROMPT_B64=<value> if present.
if [[ "${tokens[0]:-}" == OPENCLAW_PROMPT_B64=* ]]; then
    inline_b64="${tokens[0]#OPENCLAW_PROMPT_B64=}"
    # Inline value populates the env var when it is not already set.
    [[ -z "${OPENCLAW_PROMPT_B64:-}" ]] && OPENCLAW_PROMPT_B64="$inline_b64"
    tokens=( "${tokens[@]:1}" )
elif [[ "${tokens[0]:-}" == *=* && "${tokens[0]:-}" != */* ]]; then
    die "inline env-var '${tokens[0]%%=*}' is not permitted; only OPENCLAW_PROMPT_B64 is accepted"
fi

[[ "${tokens[0]:-}" == "vibe" ]] \
    || die "command must be 'vibe'; got '${tokens[0]:-}'"

workdir=""
prompt_token=""
i=1

while [[ $i -lt ${#tokens[@]} ]]; do
    tok="${tokens[$i]}"
    case "$tok" in
        --workdir)
            i=$(( i + 1 ))
            [[ $i -lt ${#tokens[@]} ]] || die "--workdir requires a value"
            workdir="${tokens[$i]}"
            i=$(( i + 1 ))
            ;;
        --prompt|-p)
            i=$(( i + 1 ))
            [[ $i -lt ${#tokens[@]} ]] || die "${tok} requires a value"
            prompt_token="${tokens[$i]}"
            i=$(( i + 1 ))
            ;;
        *) die "unexpected token '${tok}' in command" ;;
    esac
done

[[ -n "$workdir"      ]] || die "--workdir is required"
[[ -n "$prompt_token" ]] || die "--prompt / -p is required"

# The sentinel token must literally be __B64__ to signal that the real prompt
# is in OPENCLAW_PROMPT_B64.  Any other value is rejected.
[[ "$prompt_token" == "__B64__" ]] \
    || die "--prompt value must be '__B64__' (use OPENCLAW_PROMPT_B64 env var)"

# ── decode and validate prompt ────────────────────────────────────────────────

prompt_b64="${OPENCLAW_PROMPT_B64:-}"
[[ -n "$prompt_b64" ]] || die "OPENCLAW_PROMPT_B64 env var is empty or not set"

decoded_prompt="$(printf '%s' "$prompt_b64" | base64 --decode 2>/dev/null)" \
    || die "OPENCLAW_PROMPT_B64 is not valid base64"
[[ -n "$decoded_prompt" ]] || die "decoded prompt is empty"

# ── validate workdir ──────────────────────────────────────────────────────────

# Must be absolute.
[[ "$workdir" == /* ]] || die "--workdir must be absolute; got '${workdir}'"

# Must exist on the host.
[[ -d "$workdir" ]] || die "workdir does not exist: ${workdir}"

# Must not contain path traversal sequences.
[[ "$workdir" != *..* ]] || die "--workdir must not contain '..'"

# Must match an entry in the allowlist.
[[ -f "$ALLOWLIST" ]] || die "allowlist not found: ${ALLOWLIST}"

allowed=0
while IFS= read -r entry || [[ -n "$entry" ]]; do
    # Skip blank lines and comments.
    [[ -z "$entry" || "$entry" == \#* ]] && continue
    if [[ "$workdir" == "$entry" || "$workdir" == "${entry}/"* ]]; then
        allowed=1
        break
    fi
done < "$ALLOWLIST"

[[ $allowed -eq 1 ]] \
    || die "workdir '${workdir}' is not in the allowlist ${ALLOWLIST}"

log "ALLOWED: workdir=${workdir}"

# ── acquire lock ──────────────────────────────────────────────────────────────

[[ -x "$RUNNER" ]] || die "runner not found or not executable: ${RUNNER}"

# Use flock(1) with a timeout; lock is released automatically when the
# subshell exits (whether normally or on signal).
(
    flock --exclusive --timeout "$LOCK_TIMEOUT" 200 \
        || die "another vibe run is already in progress (could not acquire lock within ${LOCK_TIMEOUT}s)"

    log "lock acquired; starting container run"

    # Export decoded env for the runner; runner passes it into the container.
    export OPENCLAW_PROMPT_B64="$prompt_b64"

    if command -v timeout >/dev/null 2>&1; then
        exec timeout "$MAX_RUNTIME" "$RUNNER" --workdir "$workdir"
    else
        exec "$RUNNER" --workdir "$workdir"
    fi
) 200>"$LOCK_FILE"
