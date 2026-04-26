"""openclaw_control/evidence_pipeline.py

Operator-grade evidence → analysis → action helpers for the Team Review
orchestrators.  All I/O is injected via callables; this module contains no
circular imports with service.py.

Public API
----------
VIBE_REQUEST_RE : re.Pattern
    Detects VIBE_REPORT_REQUEST tokens in agent output.

run_with_evidence(agent, prompt, run, *, push_event, run_for_team,
                  run_vibe_probe, known_report_ids, timeout_s,
                  ssh_configured, agent_key) -> tuple[str, bool]
    Run an agent, intercept any VIBE_REPORT_REQUEST token, execute the SSH
    probe (emitting feed events), and re-run the agent once with the live
    data injected.  Returns (final_output, is_error).

dispatch_coo_action(coo_output, run, *, push_event, github_repo,
                    allowed_repos) -> None
    Emit a "proposal" feed event for every actionable item in the COO memo.
    The operator must confirm (or cancel) each one via the UI — no issues are
    created automatically.
"""

from __future__ import annotations

import re

from trigger_happy_proposals import extract_proposals

# Matches VIBE_REPORT_REQUEST: <report_id> in agent output.
VIBE_REQUEST_RE = re.compile(r"VIBE_REPORT_REQUEST:\s*(\w+)", re.IGNORECASE)


def run_with_evidence(
    agent,
    prompt: str,
    run: dict,
    *,
    push_event,
    run_for_team,
    run_vibe_probe,
    known_report_ids: set,
    timeout_s: float,
    ssh_configured: bool,
    agent_key: str,
) -> tuple[str, bool]:
    """Run *agent* with VIBE_REPORT_REQUEST follow-up for team review.

    If the first agent pass emits ``VIBE_REPORT_REQUEST: <id>``, this function:
    1. Emits a probe-start feed event announcing the data fetch.
    2. Executes the SSH probe via *run_vibe_probe*.
    3. Re-runs the agent once with the probe data injected.

    Falls back to the original first-pass output if the re-run fails or times
    out, so callers always receive a usable response.

    Returns (final_output, is_error).
    """
    output, is_error = run_for_team(agent, prompt, timeout_s)
    if is_error or not ssh_configured:
        return output, is_error

    match = VIBE_REQUEST_RE.search(output)
    if not match:
        return output, is_error

    report_id = match.group(1).lower()
    if report_id not in known_report_ids:
        return output, is_error

    push_event(run, agent_key, "message", f"🔬 Fetching live data: {report_id}…")
    probe_data = run_vibe_probe(report_id)

    augmented = (
        f"{prompt}\n\n"
        f"=== VIBE REPORT: {report_id} ===\n{probe_data}\n"
        f"=== END VIBE REPORT ===\n"
        f"Now answer using the report data above."
    )
    output2, is_error2 = run_for_team(agent, augmented, timeout_s)
    if is_error2:
        # Fall back to original output rather than returning an error sentinel.
        return output, is_error

    push_event(run, agent_key, "message", f"  ↳ Live {report_id} data injected ✓")
    return output2, False


def dispatch_coo_action(
    coo_output: str,
    run: dict,
    *,
    push_event,
    github_repo: str | None = None,
    allowed_repos: frozenset = frozenset(),
) -> None:
    """Propose a GitHub issue for every actionable item in the COO memo.

    Each numbered next-action item (and any /copilot task sentence) becomes a
    ``"proposal"`` feed event.  The operator must explicitly confirm (or cancel)
    each one via the UI — the system never auto-creates issues without consent.

    When ``github_repo`` is absent or empty the proposal events carry
    ``repo_ambiguous=True`` so the frontend can ask the operator to pick the
    target repo once, rather than guessing.

    Emits no events silently when no actionable items are found.
    """
    if not coo_output:
        return

    proposals = extract_proposals(coo_output, github_repo, allowed_repos)
    if not proposals:
        return

    for p in proposals:
        push_event(
            run, "coo", "proposal",
            f"📋 Proposed: {p['title']}",
            title=p["title"],
            body=p["body"],
            repo=p["repo"],
            repo_ambiguous=p["repo_ambiguous"],
            allowed_repos=p["allowed_repos"],
        )

