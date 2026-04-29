#!/usr/bin/env bash
# vibe-gateway/bin/run-vibe-container.sh — hardened Docker runner for vibe
#
# Installed to: /opt/openclaw/gateway/bin/run-vibe-container.sh
# Owner:        root:root  mode 0755
#
# Invoked exclusively by forced-command.sh.  Accepts:
#   --workdir /absolute/path
#
# The prompt is read from OPENCLAW_PROMPT_B64 (set by forced-command.sh).
#
# Container security defaults:
#   --rm                      remove container after exit
#   --cap-drop=ALL            drop all Linux capabilities
#   --security-opt=no-new-privileges   prevent privilege escalation
#   --pids-limit              cap process count inside container
#   --memory                  cap memory usage
#   --cpus                    cap CPU quota
#   --read-only               read-only root filesystem
#   --tmpfs /tmp              writable tmp for vibe scratch space
#
# Persistent vibe config/cache is stored under /var/lib/openclaw-vibe/ on the
# host and bind-mounted read-write into the container.
#
# Model / API environment variables (MISTRAL_API_KEY, etc.) are forwarded only
# if they are set; no defaults are hardcoded.  See README §Configuration.

set -euo pipefail

readonly PROG="run-vibe-container"
readonly IMAGE="${OPENCLAW_VIBE_IMAGE:-openclaw-vibe-gateway:latest}"
readonly CACHE_DIR="${OPENCLAW_VIBE_CACHE_DIR:-/var/lib/openclaw-vibe/cache}"
readonly SSH_DIR="${OPENCLAW_VIBE_SSH_DIR:-/var/lib/openclaw-vibe/.ssh}"
readonly PIDS_LIMIT="${OPENCLAW_VIBE_PIDS_LIMIT:-64}"
readonly MEMORY="${OPENCLAW_VIBE_MEMORY:-2g}"
readonly CPUS="${OPENCLAW_VIBE_CPUS:-1.0}"
readonly CONTAINER_USER="${OPENCLAW_VIBE_CONTAINER_USER:-1500:1500}"

die() { printf '%s: error: %s\n' "$PROG" "$*" >&2; exit 1; }

# ── parse arguments ───────────────────────────────────────────────────────────

workdir=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --workdir)
            shift
            [[ $# -gt 0 ]] || die "--workdir requires a value"
            workdir="$1"
            shift
            ;;
        --workdir=*)
            workdir="${1#*=}"
            shift
            ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ -n "$workdir" ]] || die "--workdir is required"
[[ "$workdir" == /* ]] || die "--workdir must be absolute; got '${workdir}'"
[[ -d "$workdir" ]] || die "workdir does not exist: ${workdir}"

# ── prompt validation ─────────────────────────────────────────────────────────

prompt_b64="${OPENCLAW_PROMPT_B64:-}"
[[ -n "$prompt_b64" ]] || die "OPENCLAW_PROMPT_B64 is not set"

# ── ensure cache directory exists ─────────────────────────────────────────────

mkdir -p "$CACHE_DIR"

# ── build docker run arguments ────────────────────────────────────────────────

docker_args=(
    run
    --rm
    --cap-drop=ALL
    --security-opt=no-new-privileges
    --pids-limit="$PIDS_LIMIT"
    --memory="$MEMORY"
    --cpus="$CPUS"
    --user="$CONTAINER_USER"
    --read-only
    --tmpfs=/tmp:rw,noexec,nosuid,size=256m
    --mount "type=bind,source=${workdir},target=/work"
    --mount "type=bind,source=${CACHE_DIR},target=/home/vibeuser/.cache/vibe"
    --env "OPENCLAW_PROMPT_B64=${prompt_b64}"
    --network=host
)

# Bind-mount the SSH directory so vibe can authenticate to openclaw-readonly
# for read-only probe actions (uptime, docker ps, etc.).  The mount is
# read-only inside the container; the container never writes to ~/.ssh.
if [[ -d "$SSH_DIR" ]]; then
    docker_args+=(
        --mount "type=bind,source=${SSH_DIR},target=/home/vibeuser/.ssh,readonly"
    )
fi

# Forward model / API env vars when present (never hardcode secrets).
for var in MISTRAL_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY \
           VIBE_MODEL VIBE_MAX_TOKENS VIBE_TEMPERATURE; do
    if [[ -n "${!var:-}" ]]; then
        docker_args+=( --env "${var}=${!var}" )
    fi
done

docker_args+=( "$IMAGE" )

# ── run ───────────────────────────────────────────────────────────────────────

exec docker "${docker_args[@]}"
