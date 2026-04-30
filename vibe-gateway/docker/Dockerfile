# vibe-gateway/docker/Dockerfile
#
# Builds a minimal image that installs the Mistral `vibe` CLI and runs it as a
# non-root user.  The prompt is delivered via OPENCLAW_PROMPT_B64 (base64) and
# the working directory is bind-mounted to /work at runtime.
#
# Build:
#   docker build -t openclaw-vibe-gateway:latest vibe-gateway/docker/
#
# Run (via run-vibe-container.sh — do not invoke docker directly):
#   OPENCLAW_PROMPT_B64=<b64> ./vibe-gateway/bin/run-vibe-container.sh \
#       --workdir /srv/openclaw-work

FROM ubuntu:24.04

# ── system dependencies ───────────────────────────────────────────────────────

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        openssh-client \
        python3 \
        python3-pip \
 && rm -rf /var/lib/apt/lists/*

# ── install vibe (Mistral Vibe CLI) ──────────────────────────────────────────
# Install system-wide as root so every user (including vibeuser) can execute it.
# Ubuntu 24.04 ships pip ≥ 23 which requires --break-system-packages for
# system-wide installs outside a virtualenv.

RUN pip3 install --no-cache-dir --break-system-packages vibe-cli \
 && vibe --version

# ── non-root user ─────────────────────────────────────────────────────────────

RUN groupadd --gid 1500 vibeuser \
 && useradd  --uid 1500 --gid vibeuser --shell /bin/bash \
             --home /home/vibeuser --create-home vibeuser

# ── working directory ─────────────────────────────────────────────────────────

WORKDIR /work

# ── entrypoint ────────────────────────────────────────────────────────────────

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

USER vibeuser

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
