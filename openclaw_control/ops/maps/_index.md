# Per-service container map cards

## Purpose

Read-only inventory of containers running on the operator's VPS (`srv1501082`)
as observed during the **Phase-0 discovery pass on 2026-04-27**. These cards
are inventory documentation — factual snapshots only — with no remediation,
no env values, and no secrets.

They sit **alongside** `openclaw_control_ops_map.yaml` (the file loaded by
`map_loader.py` as agent core memory) and are **intentionally not yet wired**
into the loader. Wiring is a follow-up decision once the schema settles.

## Files

| File | Service(s) | Container(s) |
|---|---|---|
| `linkedin.yml`        | LinkedIn Data Centre News                       | `linkedin_data_centre_news-linkedin-cron-1`, `linkedin_data_centre_news-linkedin-web-1` |
| `openclaw_crypto.yml` | Crypto trading orchestrator                     | `openclaw-orchestrator`                                                                  |
| `alpaca_orb.yml`      | Alpaca ORB equities bot                         | `alpaca_orb_bite_bot`                                                                    |
| `openclaw_oss.yml`    | Upstream OpenClaw (Clawdbot/Moltbot) AI app     | `openclaw-1ne6-openclaw-1`, `openclaw-1ne6-openclaw-cron-1`                              |
| `traefik.yml`         | Reverse proxy / TLS                             | `traefik-traefik-1`                                                                      |

## Discovery method

Discovery was executed through the cockpit's existing SSH path to
`jacks@srv1501082` using read-only commands only:
`docker ps`, `docker inspect`, `docker logs --tail`, `find`, `git remote / git log`.
No state was modified on the VPS. Approval surface (Telegram) was not used
because no mutative actions were performed.

## Schema

Each card uses the same shape:

```yaml
card: <service slug>
discovery:
  date: 2026-04-27
  source: phase-0 read-only ssh discovery
services:
  - name: ...
    container_name: ...
    image: ...
    status_at_discovery: ...
    restart_policy: ...
    health: ...
    working_dir: ...
    cmd: ...
    entrypoint: ...
    ports: [...]
    mounts_count: <int>
    role: >
      ...
    observed_runtime_signals: [...]
    observed_warnings: [...]
host:
  compose_files_observed: [...]
  source_repo: ...
  presumed_live_repo_path: ...
  presumed_live_repo_head: ...
assumptions: [...]
unknowns: [...]
next_inspection_questions: [...]
```

Where a fact was not captured in this pass, it is listed under `unknowns`
with a concrete `next_inspection_questions` entry to answer it later.

## What is intentionally NOT here

- API key values, env var values, any secret data.
- Remediation steps, fix commits, or change proposals.
- Any speculation beyond `assumptions` explicitly labelled as such.

## Follow-up integration (future, separate PR)

- Extend `map_loader.py` to merge multiple YAML files so agents can introspect
  non-primary containers via `get_summary()`.
- Re-run discovery after stale-clone cleanup (8 git clones across 5 directories
  for 3 repos were observed; flagged in `assumptions`/`unknowns`).
- Append answers to `next_inspection_questions` as future passes resolve them.
