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
                    allowed_repos, seen_fps) -> list[str]
    Emit a "proposal" feed event for every actionable item in the COO memo.
    The operator must confirm (or cancel) each one via the UI — no issues are
    created automatically.  Returns the fingerprints of emitted proposals so
    callers can record them for 24h deduplication.

    When no actionable items are found, emits a ``"no-action"`` event.
    When the target repo is ambiguous (>1 candidate), emits an
    ``"action_pending"`` event before the proposal cards.
"""

from __future__ import annotations

import re

from trigger_happy_proposals import extract_proposals, proposal_fingerprint

# Matches VIBE_REPORT_REQUEST: <report_id> in agent output.
VIBE_REQUEST_RE = re.compile(r"VIBE_REPORT_REQUEST:\s*(\w+)", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Repo candidate detection
# ---------------------------------------------------------------------------
# Maps each allowed repo to a list of lowercase keyword signals.  A repo is
# considered a candidate when at least one of its signals appears (as a full
# word or word-prefix) in the COO memo text.  Signals that are substrings of
# unrelated words (e.g. "log" inside "logic") are avoided by choosing terms
# specific enough that false matches are rare.
_REPO_SIGNALS: dict[str, list[str]] = {
    "leeheggan-droid/openclaw-crypto": [
        "crypto", "orchestrator", "openclaw-orchestrator", "trading", "trade",
        "pnl", "quant", "signal", "halt", "docker", "container", "logging", "logs",
    ],
    "leeheggan-droid/openclaw-control": [
        "control", "ui", "web", "dashboard", "frontend", "logging", "logs",
        "panel", "openclaw-control", "persistent",
    ],
    "leeheggan-droid/alpaca_orb_bite_bot": [
        "alpaca", "broker", "alpaca_orb", "orb_bite",
    ],
    "leeheggan-droid/LinkedIn_Data_Centre_News": [
        "linkedin", "data centre", "news scrape", "scraper",
    ],
}

# Pre-compiled patterns for each signal (word-boundary prefix match).
_REPO_SIGNAL_RES: dict[str, list[re.Pattern]] = {
    repo: [re.compile(r"\b" + re.escape(sig), re.IGNORECASE) for sig in sigs]
    for repo, sigs in _REPO_SIGNALS.items()
}


def _candidate_repos(coo_output: str, allowed_repos: frozenset) -> list[str]:
    """Return allowed repos that are positively signalled by *coo_output* text.

    Each signal must appear at a word boundary to avoid substring false
    matches (e.g. "log" in "logic").  Falls back to ``sorted(allowed_repos)``
    when no keywords match, ensuring the operator is always shown a complete
    list rather than an empty one.
    """
    if not coo_output or not allowed_repos:
        return sorted(allowed_repos)

    matched: list[str] = []
    for repo, patterns in _REPO_SIGNAL_RES.items():
        if repo not in allowed_repos:
            continue
        if any(p.search(coo_output) for p in patterns):
            matched.append(repo)

    return sorted(matched) if matched else sorted(allowed_repos)


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
    seen_fps: set[str] | None = None,
) -> list[str]:
    """Propose a GitHub issue for every actionable item in the COO memo.

    Each numbered next-action item (and any /copilot task sentence) becomes a
    ``"proposal"`` feed event.  The operator must explicitly confirm (or cancel)
    each one via the UI — the system never auto-creates issues without consent.

    Target repo resolution (no guessing):
      * When *github_repo* is explicitly supplied it is used as-is.
      * Otherwise, candidate repos are derived from keyword signals in the
        COO memo text.  If exactly one candidate matches it is pre-selected.
        If more than one match, an ``"action_pending"`` event is emitted first
        so the operator knows to pick a repo on each proposal card.
      * When no signals match, all *allowed_repos* are presented.

    Emits a ``"no-action"`` event when no actionable items are found.

    ``seen_fps`` is an optional set of 24h-scoped fingerprints; proposals
    whose fingerprint is already in this set are suppressed.

    Returns the fingerprints of the proposals that were emitted so callers
    can record them for deduplication.
    """
    if not coo_output:
        push_event(run, "coo", "no-action", "✓ No memo content — nothing to propose.")
        return []

    # Determine target repo and candidate set from content signals.
    if github_repo:
        # Explicitly supplied repo: honour it, no ambiguity.
        resolved_repo: str | None = github_repo
        candidate_set: frozenset = frozenset([github_repo]) & allowed_repos or frozenset([github_repo])
    else:
        candidates = _candidate_repos(coo_output, allowed_repos)
        if len(candidates) == 1:
            resolved_repo = candidates[0]
            candidate_set = frozenset(candidates)
        else:
            # Multiple (or zero) candidates — do not guess.
            resolved_repo = None
            candidate_set = frozenset(candidates) if candidates else allowed_repos

    proposals = extract_proposals(coo_output, resolved_repo, candidate_set, seen_fps)

    if not proposals:
        push_event(run, "coo", "no-action", "✓ COO memo reviewed — no actionable items found.")
        return []

    # Emit action_pending when the operator must still choose a repo.
    if resolved_repo is None:
        push_event(
            run, "coo", "action_pending",
            f"📌 {len(proposals)} proposal(s) ready — please select a target repo in each card below.",
        )

    emitted_fps: list[str] = []
    for p in proposals:
        push_event(
            run, "coo", "proposal",
            f"📋 Proposed: {p['title']}",
            title=p["title"],
            body=p["body"],
            repo=p["repo"],
            repo_ambiguous=p["repo_ambiguous"],
            allowed_repos=p["allowed_repos"],
            fingerprint=p.get("fingerprint", ""),
        )
        if p.get("fingerprint"):
            emitted_fps.append(p["fingerprint"])

    return emitted_fps

