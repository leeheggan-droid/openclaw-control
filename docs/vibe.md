# OpenClaw Vibe — Execution Contract

This document defines the mechanical execution contract for mediated VPS access
via the Vibe client shim. It describes **interface and behaviour only**; no live
credentials, keys, or server-side wiring are included or activated.

---

## Overview

`tools/vibe-openclaw` is a thin shell client that forwards a single `vibe`
invocation to a remote VPS over SSH. Every execution attempt must be
**explicitly approved by a human operator** before the SSH connection is opened.
The shim itself enforces no approval; the approval gate is implemented by the
OpenClaw web UI (see [Vibe Execution Gateway](../vibe-execution-gateway.md)).

---

## Execution Contract

### Pre-conditions (all must hold before the shim is called)

| # | Pre-condition |
|---|---|
| 1 | `OPENCLAW_SSH_HOST` is set to a reachable SSH target (`user@host`). |
| 2 | The operator's SSH key is authorised on the remote host (`~/.ssh/authorized_keys`). |
| 3 | The `vibe` CLI is installed and on `$PATH` on the remote host. |
| 4 | A human operator has reviewed and confirmed the `workdir` and `prompt` values in the UI. |

### Invocation signature

```
vibe-openclaw --workdir <path> --prompt <text>
```

| Argument | Required | Description |
|---|---|---|
| `--workdir` | Yes (or `OPENCLAW_VIBE_WORKDIR`) | Absolute path on the VPS where `vibe` will operate. |
| `--prompt` | Yes | Free-text instruction forwarded verbatim to `vibe`. |

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENCLAW_SSH_HOST` | *(none — required)* | SSH target, e.g. `user@203.0.113.10`. |
| `OPENCLAW_VIBE_WORKDIR` | `""` | Fallback workdir when `--workdir` is omitted. |
| `OPENCLAW_SSH_OPTS` | `-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10` | Extra options forwarded to `ssh(1)`. |

### Remote command constructed

The shim POSIX-shell-quotes both arguments and assembles:

```
vibe --workdir <quoted-workdir> --prompt <quoted-prompt>
```

This string is passed as the remote command to `ssh`. The quoting prevents
shell-injection on the VPS side.

### Post-conditions (guaranteed on successful exit)

| # | Post-condition |
|---|---|
| 1 | The shim exits with the same exit code as the remote `vibe` process. |
| 2 | stdout/stderr from the remote process are streamed to the local terminal unchanged. |

### Error conditions

| Condition | Behaviour |
|---|---|
| `OPENCLAW_SSH_HOST` not set | Exits 1 with diagnostic on stderr; SSH is never attempted. |
| `--workdir` and `OPENCLAW_VIBE_WORKDIR` both empty | Exits 1 with diagnostic; SSH is never attempted. |
| `--prompt` empty | Exits 1 with diagnostic; SSH is never attempted. |
| SSH connection refused / timeout | `ssh` exits non-zero; shim propagates the exit code. |
| Remote `vibe` not found | `ssh` exits non-zero (127); shim propagates the exit code. |

---

## Security Considerations

- **No interactive prompts.** `BatchMode=yes` is set by default so the shim
  never blocks waiting for a password or host-key confirmation.
- **StrictHostKeyChecking.** `StrictHostKeyChecking=yes` is set by default.
  The remote host key must already be in `~/.ssh/known_hosts` or the connection
  is refused. Do not override this without a documented reason.
- **Argument quoting.** Both `workdir` and `prompt` are POSIX-quoted with
  `printf '%q'` before being assembled into the remote command string. This
  prevents shell-injection on the VPS.
- **Approval gate.** The shim is not self-approving. It must only be invoked
  after an explicit human confirmation step (e.g. the OpenClaw web UI confirm
  dialog). Automated agents MUST NOT call this shim without a prior
  `needs_approval=True` gate.
- **Trusted network.** Deploy OpenClaw Control on a trusted internal network.
  The shim does not authenticate the caller.

---

## Relationship to Web UI

The web UI (`web_app.py`) calls `start_vibe_run(command)` from
`openclaw_control/service.py`, which executes the SSH command directly via
`run_ssh`. The `vibe-openclaw` shim is the equivalent **CLI entry-point** for
operators who prefer to trigger Vibe runs from a terminal rather than the
browser.

Both paths share the same approval model: the operator must explicitly confirm
the `workdir` and `prompt` values before execution begins.

---

## Local Smoke-Test (no live VPS required)

```bash
# 1. Verify the shim is executable
chmod +x tools/vibe-openclaw

# 2. Check usage output (no SSH attempt)
tools/vibe-openclaw --help

# 3. Verify missing-env guard
tools/vibe-openclaw --workdir /tmp --prompt "hello"
# expected: error: OPENCLAW_SSH_HOST is not set

# 4. Verify missing-prompt guard
OPENCLAW_SSH_HOST=user@host tools/vibe-openclaw --workdir /tmp
# expected: error: --prompt is required
```
