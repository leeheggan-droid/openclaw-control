# Contributing to OpenClaw Control

This guide explains how to keep your local development environment clean so
that routine `git pull` operations never overwrite local changes, delete your
virtual environment, or leave you with a broken Python setup.

---

## Table of contents

1. [What belongs in `.gitignore` (and why)](#1-what-belongs-in-gitignore-and-why)
2. [Understanding your working-tree state before a pull](#2-understanding-your-working-tree-state-before-a-pull)
3. [Pulling safely — three common scenarios](#3-pulling-safely--three-common-scenarios)
4. [Preserving critical local files between pulls](#4-preserving-critical-local-files-between-pulls)
5. [Recreating the Python environment after a clean or fresh clone](#5-recreating-the-python-environment-after-a-clean-or-fresh-clone)
6. [File-permission issues on shared servers](#6-file-permission-issues-on-shared-servers)
7. [Quick-reference cheat sheet](#7-quick-reference-cheat-sheet)

---

## 1. What belongs in `.gitignore` (and why)

The repository's `.gitignore` already excludes the following categories.
**Never add these to git:**

| Pattern | Reason |
|---------|--------|
| `.venv/`, `venv/`, `env/` | Virtual-environment directories are large, OS-specific, and must be rebuilt locally anyway |
| `.env`, `*.env`, `.env.*` | Contains secrets — API keys, passwords, tokens |
| `*.sqlite`, `*.db`, `data/` | Local runtime databases |
| `__pycache__/`, `*.py[cod]` | Compiled Python bytecode — regenerated automatically |
| `docker-compose.override.yml` | Local Docker tweaks that must not affect other developers |
| `.idea/`, `.vscode/` | Editor configuration |

If you notice that a file you rely on locally is *not* in `.gitignore`, add it
before committing anything:

```bash
# Example: exclude a local notes file
echo "notes.txt" >> .gitignore
git add .gitignore
git commit -m "chore: ignore local notes file"
```

> **Rule of thumb:** if a file contains a secret, is generated at runtime, or
> is specific to your machine, it must not be committed.

---

## 2. Understanding your working-tree state before a pull

Always run `git status` before pulling to see exactly what has changed locally:

```bash
git status
```

The output splits into three groups:

| Section | What it means | What to do before pulling |
|---------|---------------|--------------------------|
| `Changes to be committed` | Staged edits | Commit or unstage them |
| `Changes not staged for commit` | Modified tracked files | Commit, stash, or discard |
| `Untracked files` | New files git has never seen | Stash (with `--include-untracked`), move, or delete them |

A completely clean pull requires **all three sections to be empty**.

---

## 3. Pulling safely — three common scenarios

### Scenario A — you have work-in-progress changes you want to keep

```bash
# 1. Stash your changes (including any untracked files)
git stash push --include-untracked -m "WIP before pull $(date +%Y-%m-%d)"

# 2. Pull
git pull

# 3. Re-apply your stash
git stash pop
```

If `git stash pop` reports a conflict, resolve it with your normal
merge-conflict workflow, then `git add` the resolved files and
`git stash drop` to clean up.

### Scenario B — you want to discard local changes entirely (keep nothing)

> ⚠️ **Destructive** — local edits and untracked files are permanently deleted.

```bash
# Remove all untracked files and directories (add -x to also remove ignored files)
git clean -fd

# Discard all tracked-file modifications
git reset --hard HEAD

# Pull
git pull
```

### Scenario C — permission errors during pull (files owned by root/another user)

This happens when a previous `sudo` command created files in the repo directory.

```bash
# Give yourself ownership of all files in the project
sudo chown -R $USER:$USER .

# Retry the pull
git pull
```

---

## 4. Preserving critical local files between pulls

### `.env` — your secrets file

The repo ships `config.env.example` as a template.  Your real secrets live in
`.env`, which is in `.gitignore` and will **never** be touched by a pull.

```bash
# First-time setup (one command, then edit .env)
cp config.env.example .env
$EDITOR .env    # fill in OPENAI_API_KEY, AUTH_SECRET_KEY, etc.
```

After any pull, `.env` is untouched.  You never need to recreate it.

### Docker overrides

If you have local Docker tweaks (custom ports, volume mounts, etc.), put them
in `docker-compose.override.yml` — Docker Compose merges this file
automatically and git ignores it:

```yaml
# docker-compose.override.yml  (git-ignored, safe for local changes)
services:
  openclaw:
    ports:
      - "9001:8001"
```

---

## 5. Recreating the Python environment after a clean or fresh clone

The `.venv` directory is intentionally excluded from git.  Whenever you do a
fresh clone **or** a destructive clean (`git clean -fdx`), recreate it:

```bash
# 1. Create a new virtual environment
python3 -m venv .venv

# 2. Activate it
source .venv/bin/activate          # Linux / macOS
# .venv\Scripts\activate           # Windows

# 3. Upgrade pip (optional but recommended)
pip install --upgrade pip

# 4. Install all project dependencies
pip install -r requirements.txt

# 5. Verify uvicorn is available
uvicorn --version
```

After this, the server starts normally:

```bash
uvicorn web_app:app --host 0.0.0.0 --port 9000
```

> **Note:** you must repeat steps 1–4 whenever:
> - you do a fresh clone on a new machine,
> - you run `git clean -fdx` (which removes `.venv/`), or
> - `requirements.txt` changes after a pull (re-run `pip install -r requirements.txt`).

---

## 6. File-permission issues on shared servers

On shared servers (e.g., a VPS where you sometimes `sudo`), repo files can end
up owned by `root`.  Symptoms:

```
error: unable to unlink old 'bin/some-script.sh': Permission denied
```

Fix:

```bash
# From the project root
sudo chown -R $USER:$USER .
git reset --hard HEAD
git pull
```

To prevent this in future, avoid running `git pull` or project scripts with
`sudo`.  If a script needs elevated privileges, use `sudo` only for the
specific command inside the script, not for the script invocation itself.

---

## 7. Quick-reference cheat sheet

```bash
# ── Before every pull ──────────────────────────────────────────────────────
git status                             # inspect the working tree

# ── Keep your changes ──────────────────────────────────────────────────────
git stash push --include-untracked    # stash everything
git pull                              # pull
git stash pop                         # restore your work

# ── Discard all local changes (destructive) ────────────────────────────────
git clean -fd                         # remove untracked files/dirs
git reset --hard HEAD                 # discard tracked-file edits
git pull

# ── Fix permission errors ──────────────────────────────────────────────────
sudo chown -R $USER:$USER .
git pull

# ── Recreate the Python environment ───────────────────────────────────────
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# ── First-time .env setup ──────────────────────────────────────────────────
cp config.env.example .env            # copy template
$EDITOR .env                          # fill in secrets
```
