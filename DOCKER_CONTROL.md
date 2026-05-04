# Docker Control Runner (GitHub Actions / CI)

This repository (`openclaw-control`) can be executed from a containerised control runner.
The control runner is **ephemeral** — it runs an Ansible command and exits.
The VPS always remains the **remote target**, never the control host.

## What this solves

- Avoids host Python / Ansible installation issues (e.g. Ubuntu PEP 668).
- Makes local runs and GitHub Actions runs behave the same.
- Keeps control tooling separate from runtime bot services.

## Build the control image

Run from the repository root:

```bash
docker build -t openclaw-control:ci .
