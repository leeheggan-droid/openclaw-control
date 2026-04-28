#!/usr/bin/env bash
# vibe-gateway/docker/entrypoint.sh — container entrypoint
#
# Reads the prompt from the environment variable OPENCLAW_PROMPT_B64
# (base64-encoded, set by run-vibe-container.sh), decodes it, then
# exec's `vibe --workdir /work --prompt <decoded>`.
#
# Never evaluates user-supplied strings through bash -c or eval.

set -euo pipefail

# ── decode prompt ─────────────────────────────────────────────────────────────

prompt_b64="${OPENCLAW_PROMPT_B64:-}"
[[ -n "$prompt_b64" ]] || { echo "entrypoint: OPENCLAW_PROMPT_B64 is empty" >&2; exit 1; }

prompt="$(printf '%s' "$prompt_b64" | base64 --decode)" \
    || { echo "entrypoint: base64 decode failed" >&2; exit 1; }
[[ -n "$prompt" ]] || { echo "entrypoint: decoded prompt is empty" >&2; exit 1; }

# ── exec vibe ─────────────────────────────────────────────────────────────────

exec vibe --workdir /work --prompt "$prompt"
