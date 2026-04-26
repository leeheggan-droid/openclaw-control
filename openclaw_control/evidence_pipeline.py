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

dispatch_coo_action(coo_output, run, *, push_event, github_repo) -> None
    Parse the COO decision memo for a /copilot task sentence, create a
    GitHub issue, and emit an "action" event to the run feed.
"""

from __future__ import annotations

import re

from openclaw_control.github_tools import create_github_issue

# Matches VIBE_REPORT_REQUEST: <report_id> in agent output.
VIBE_REQUEST_RE = re.compile(r"VIBE_REPORT_REQUEST:\s*(\w+)", re.IGNORECASE)

# Matches a /copilot task sentence emitted by the COO agent.
# Handles: /copilot task: X, /copilot-ready X, 4) /copilot task X …
_COPILOT_TASK_RE = re.compile(
    r"(?:^|\n)\s*(?:\d+[).]?\s*)?/copilot\S*\s*:?\s*(.+)",
    re.IGNORECASE,
)


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
) -> None:
    """Parse the COO memo for a /copilot task and create a GitHub issue.

    Emits an ``"action"`` event with the issue URL on success.  When no
    task sentence is found the function returns silently to keep the feed
    clean.  When issue creation fails (no token, API error) a brief warning
    message is emitted instead.
    """
    if not coo_output:
        return

    match = _COPILOT_TASK_RE.search(coo_output)
    if not match:
        return  # no task to dispatch — keep the feed clean

    task_sentence = match.group(1).strip()[:200]  # safety cap; title is further capped to 72 chars
    if not task_sentence:
        return

    title = task_sentence[:72]
    body = _build_action_issue_body(coo_output, task_sentence)
    repo = github_repo or "leeheggan-droid/openclaw-control"  # config default is already set by settings

    result = create_github_issue(
        title=title,
        body=body,
        repo_full=repo,
        labels=["copilot", "team-review"],
        assign_copilot=True,
    )

    if result:
        issue_url = result["issue_url"]
        push_event(
            run, "coo", "action",
            f"✅ Action: {issue_url}\n📋 Task: {task_sentence}",
        )
    else:
        push_event(
            run, "coo", "message",
            f"⚠️ GitHub issue creation failed (check GITHUB_TOKEN).\n"
            f"📋 Task: {task_sentence}",
        )


def _build_action_issue_body(coo_memo: str, task_sentence: str) -> str:
    """Build the GitHub issue body from the COO memo and extracted task."""
    memo_lines = (coo_memo or "").splitlines()
    memo_snippet = "\n".join(memo_lines[-50:])
    return (
        f"## Team Review Action\n\n"
        f"**Task:** {task_sentence}\n\n"
        f"## COO Decision Memo\n\n"
        f"```\n{memo_snippet}\n```\n\n"
        f"## Constraints\n"
        f"- No secrets or credentials added to source code\n"
        f"- No destructive operations introduced\n"
        f"- Changes limited to the minimum required\n"
        f"- Match existing code style and conventions\n\n"
        f"## Acceptance Criteria\n"
        f"- [ ] Task completed as described above\n"
        f"- [ ] Local test passed: "
        f"`git pull; uvicorn web_app:app --reload;` then verified in browser\n"
    )
