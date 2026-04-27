# PR-B Progress (harden/ssh-readonly-lane)

Lightweight heartbeat log — operator-facing visibility into branch progress.
One line per milestone or blocker. Append-only.

> Note on ETAs: per the operating policy I work under, I do not invent
> time-in-minutes estimates. The `next:` field describes the immediate
> concrete step instead. If a milestone is expected to be unusually
> long-running, the heartbeat line will say so explicitly.

---

2026-04-27T13:05:34Z anchor push; next: modify openclaw_control/service.py (add run_ssh_readonly helper, route run_vibe_report + _gather_vibe_snapshot + _ap_ssh to it, add start_readonly_run/get_readonly_run for terminal pills)
2026-04-27 13:08Z - done: openclaw_control/config.py (added ssh_readonly_host setting, falls back to ssh_host when unset for backwards compat) - next: openclaw_control/service.py (add run_ssh_readonly helper accepting optional host param, route 3 read-only callers to readonly lane, add start_readonly_run/get_readonly_run for terminal-pill flow)
