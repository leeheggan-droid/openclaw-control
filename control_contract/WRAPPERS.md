# VPS Wrapper Contract (OpenClaw)

This defines the supported interface between the OpenClaw control plane and the VPS runtime.

## Registry
- `/opt/control/services.json` on the VPS is the authoritative registry.
- This repo carries a copy at `control_contract/services.json` to document the contract.

## Wrapper commands (VPS)
All commands live at: `/opt/control/bin`

| Action | Command | Output |
|---|---|---|
| List | `svc-list` | tab-delimited text |
| Status | `svc-status <id>` | JSON |
| Start | `svc-start <id>` | JSON |
| Stop | `svc-stop <id>` | JSON |
| Restart | `svc-restart <id>` | JSON |
| Logs | `svc-logs <id> [n]` | text |
| Run oneshot job | `svc-run <id>` | JSON (timer-backed jobs only) |

## Rules
- Control plane MUST NOT call `systemctl` directly.
- Parse JSON strictly and treat non-zero exit code as failure.
- Timers must NOT be started via `svc-start`.
- Use `svc-run` to trigger a timer-backed oneshot job (e.g. LinkedIn).
