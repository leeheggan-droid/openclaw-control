# Control image for running Ansible playbooks in GitHub Actions CI
# Includes: ansible + ssh client
# Secrets are injected at runtime (do not bake keys into the image)

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# SSH client is required because Ansible uses ssh to reach the VPS
RUN apt-get update && apt-get install -y --no-install-recommends \
      openssh-client \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Ansible inside the container
RUN python -m pip install --no-cache-dir ansible

WORKDIR /work

# Prepare SSH directory; keys/known_hosts will be mounted at runtime
RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh

ENTRYPOINT ["ansible-playbook"]
