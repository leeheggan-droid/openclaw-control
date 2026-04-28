# OpenClaw Agent — Anthropic (Claude) + Vibe Integration Setup

This document describes how to wire the OpenClaw VPS Agent with Anthropic
(Claude) as the LLM provider and the Vibe Execution Gateway for safe,
allowlisted VPS operations.

---

## Architecture overview

```
GitHub issues (agent:queue)
        │
        │  poll every POLL_INTERVAL_SECONDS
        ▼
  openclaw-agent (VPS systemd service)
        │
        ├─ LLM call (Anthropic / Gemini / Groq)
        │       └─ pre-approval plan → issue comment
        │
        │  await /approve comment
        │
        ├─ LLM call (JSON ops list)
        │
        ├─ Vibe Execution Gateway (allowlist validated)
        │       └─ restart_service | tail_journal | read_file | …
        │
        └─ GitHub PR (run artifact) + issue labels
```

Label state machine:

```
agent:queue → agent:working → agent:needs-approval → agent:done
```

---

## 1. Prerequisites

- VPS running Ubuntu 22.04+ with Python 3.10+.
- `python3-dotenv` and `requests` installed (see `requirements.txt`).
- An Anthropic API key from <https://console.anthropic.com/>.
- A GitHub Personal Access Token with `repo` scope.
- (Optional) Vibe HTTP gateway or CLI at `/usr/local/bin/vibe`.

---

## 2. Install the agent on the VPS

```bash
# Clone the repo
sudo mkdir -p /opt/openclaw-control
cd /opt/openclaw-control
git clone https://github.com/leeheggan-droid/openclaw-control.git .

# Install Python dependencies
pip3 install -r requirements.txt

# Create the dedicated service user
sudo useradd -r -s /usr/sbin/nologin openclaw-agent

# Set up config
sudo mkdir -p /etc/openclaw-agent
sudo cp config.env.example /etc/openclaw-agent/config.env
sudo chmod 600 /etc/openclaw-agent/config.env
sudo chown root:root /etc/openclaw-agent/config.env
```

---

## 3. Configure the agent

Edit `/etc/openclaw-agent/config.env` (never commit real values to source control):

```ini
# GitHub
GITHUB_TOKEN=ghp_your_real_token_here
GITHUB_REPO=leeheggan-droid/openclaw-control

# LLM provider
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-your_real_key_here
ANTHROPIC_MODEL=claude-3-haiku-20240307

# Vibe executor (HTTP gateway preferred; CLI as fallback)
VIBE_API_BASE=http://127.0.0.1:7000
VIBE_API_KEY=your_vibe_key_here
VIBE_ALLOWLIST=restart_service:openclaw-agent.service,tail_journal:openclaw-agent.service

# Polling
POLL_INTERVAL_SECONDS=30
APPROVAL_TIMEOUT_SECONDS=3600
```

### Switching to Gemini

```ini
LLM_PROVIDER=gemini
GEMINI_API_KEY=AIza_your_real_key_here
GEMINI_MODEL=gemini-1.5-flash
```

### Switching to Groq

```ini
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_your_real_key_here
GROQ_MODEL=llama-3.3-70b-versatile
```

---

## 4. Install the systemd service

```bash
sudo cp /opt/openclaw-control/systemd/openclaw-agent.service \
        /etc/systemd/system/openclaw-agent.service

sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-agent
sudo systemctl status openclaw-agent
```

View logs:

```bash
journalctl -u openclaw-agent -f
```

---

## 5. GitHub label setup

Create these labels in your repository (Settings → Labels):

| Label                  | Colour  | Meaning                                 |
|------------------------|---------|-----------------------------------------|
| `agent:queue`          | `#0075ca` | Issue is waiting to be picked up      |
| `agent:working`        | `#e4e669` | Agent is actively processing           |
| `agent:needs-approval` | `#d93f0b` | Awaiting human `/approve` comment      |
| `agent:done`           | `#0e8a16` | Processing complete                     |

---

## 6. Vibe executor allowlist

The `VIBE_ALLOWLIST` environment variable controls which operations the agent
is permitted to execute.  The format is a comma-separated list of
`action_type:target` pairs.

| Action type       | Target example                   | Effect                              |
|-------------------|----------------------------------|-------------------------------------|
| `restart_service` | `openclaw-agent.service`         | Restart a systemd unit              |
| `tail_journal`    | `openclaw-agent.service`         | Return recent journal entries       |
| `read_file`       | `/var/log/syslog`                | Read a file path                    |

Using `action_type:*` (e.g. `tail_journal:*`) permits that action against any
target — use with caution.

**Deny-by-default**: any operation not present in the allowlist is blocked
before it reaches the Vibe gateway.  The denial is logged and posted as an
issue comment.

---

## 7. Audit logging

Every Vibe call produces a JSON audit log line at `INFO` level:

```json
{
  "ts":         "2026-04-28T06:00:00+00:00",
  "action":     "tail_journal",
  "target":     "openclaw-agent.service",
  "params":     {},
  "exit_code":  0,
  "stdout_len": 1234,
  "stderr_len": 0
}
```

When a call is denied the `"denied": true` field is added and `exit_code` is 1.

These records appear in the systemd journal and are queryable with:

```bash
journalctl -u openclaw-agent | grep VIBE_AUDIT
```

---

## 8. Safety principles

1. **No mutation without `/approve`** — The agent never creates branches,
   commits, or PRs until a human has commented `/approve` on the issue.

2. **All VPS ops through Vibe** — The agent never calls `subprocess` directly.
   All operations go through `vibe_client.vibe_call()`, which enforces the
   allowlist before any network call is made.

3. **Deny-by-default** — Unknown action types and targets are rejected at the
   allowlist check, not at the Vibe gateway.

4. **No secrets in comments or logs** — The agent never includes API keys,
   tokens, or other credentials in issue comments, PR bodies, or log output.

5. **Least privilege** — The systemd service runs as a dedicated non-root
   `openclaw-agent` user.  The config file is `chmod 600` and readable only
   by root / the service user.

6. **Audit trail** — Every Vibe call (allowed or denied) is logged to the
   systemd journal in structured JSON.

---

## 9. Hardening recommendations

- Run the Vibe HTTP gateway on `127.0.0.1` only (never expose to the internet).
- Set `VIBE_ALLOWLIST` as narrowly as possible — add entries one at a time as
  you confirm each operation is safe.
- Rotate `ANTHROPIC_API_KEY` and `GITHUB_TOKEN` regularly.
- Use a GitHub fine-grained token scoped to only the target repository.
- Enable GitHub branch protection on `main` so the agent's run-artifact PRs
  require a human review before merge.
- Periodically audit `journalctl -u openclaw-agent | grep VIBE_AUDIT` to
  confirm only expected operations are being executed.

---

## 10. Verify Anthropic connectivity from the VPS

```bash
curl -s https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-3-haiku-20240307","max_tokens":16,"messages":[{"role":"user","content":"ping"}]}' \
  | python3 -m json.tool
```

Expected: a JSON response containing `"type": "message"` and a short reply.

---

## 11. Test end-to-end

1. Create a GitHub issue in the configured repository.
2. Add the `agent:queue` label.
3. Watch the agent logs: `journalctl -u openclaw-agent -f`.
4. The agent should:
   - Remove `agent:queue`, add `agent:working`.
   - Post a plan comment within ~30 s.
   - Add `agent:needs-approval`.
5. Comment `/approve` on the issue.
6. The agent should:
   - Execute the allowlisted ops via Vibe.
   - Post a results comment.
   - Open a run-artifact PR.
   - Add `agent:done`.
