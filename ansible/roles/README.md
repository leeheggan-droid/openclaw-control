# =============================================================================
# ansible/roles/README.md — Future Ansible Roles
# =============================================================================
# This directory is reserved for Ansible roles as the control layer grows.
#
# What is a role?
# ---------------
# An Ansible role is a reusable, self-contained unit of automation that
# bundles tasks, handlers, variables, templates, and files together under a
# conventional directory layout.  Roles make large playbooks easier to
# read, test, and share.
#
# When to add a role
# ------------------
# Consider extracting a role when you find yourself repeating a set of tasks
# across multiple playbooks, or when a concern (e.g. "install Docker",
# "harden SSH", "deploy a specific bot") grows complex enough to deserve its
# own directory.
#
# Creating a role
# ---------------
# Use the ansible-galaxy scaffolding tool to generate the standard layout:
#
#   cd ansible/
#   ansible-galaxy role init roles/<role-name>
#
# This creates:
#   roles/<role-name>/
#     tasks/main.yml       — entry point (required)
#     handlers/main.yml    — handlers triggered by notify
#     defaults/main.yml    — default variables (lowest priority)
#     vars/main.yml        — role-specific variables (higher priority)
#     files/               — static files to copy to hosts
#     templates/           — Jinja2 templates
#     meta/main.yml        — role metadata and dependencies
#     README.md            — role documentation
#
# Using a role in site.yml
# ------------------------
# Add the role to the `roles:` block in your play, e.g.:
#
#   - name: Manage Docker stack on VPS
#     hosts: vps
#     roles:
#       - role: roles/docker-stack
#
# Example role ideas for openclaw-control
# ----------------------------------------
#   roles/docker-install   — Ensure Docker CE + Compose v2 are installed
#   roles/deploy-bots      — Full deploy workflow for the bot containers
#   roles/monitoring       — Set up basic container health monitoring/alerts
#   roles/harden-ssh       — Baseline SSH hardening (disable root, key-only)
#   roles/firewall         — Configure UFW rules on the VPS
# =============================================================================
