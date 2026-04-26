"""trigger_happy_proposals.py

Operator-grade GitHub issue proposal extraction for Team Review next actions.

The system proposes a GitHub issue for EVERY actionable item found in the COO
decision memo.  The operator confirms (or cancels) each one — the system never
auto-creates issues without an explicit operator confirmation.

When the target repository is ambiguous (settings.github_repo is empty or not
set), proposal events include ``repo_ambiguous=True`` and the full allowed_repos
list so the UI can ask the operator to pick once, not on every action.

Public API
----------
extract_proposals(coo_output, github_repo, allowed_repos) -> list[dict]
    Parse the COO memo and return proposal dicts ready to emit as feed events.

build_proposal_issue_body(coo_memo, action_text) -> str
    Build a GitHub issue body from the COO memo and action text.
"""

from __future__ import annotations

import re

# Matches the "Next actions" section and its numbered items.
# Handles optional label variants: "Next actions:", "Next actions (max 3):", etc.
_NEXT_ACTIONS_BLOCK_RE = re.compile(
    r"Next actions[^:\n]*:?\s*\n((?:[ \t]*\d+[).]\s*.+\n?)+)",
    re.IGNORECASE,
)
_ACTION_LINE_RE = re.compile(r"^[ \t]*\d+[).]\s*(.+)$")

# Matches explicit /copilot task sentences (legacy trigger, kept for compatibility).
_COPILOT_TASK_RE = re.compile(
    r"(?:^|\n)\s*(?:\d+[).]?\s*)?/copilot\S*\s*:?\s*(.+)",
    re.IGNORECASE,
)

# Minimum action text length to skip trivially short or empty lines.
_MIN_ACTION_LEN = 8


def extract_proposals(
    coo_output: str,
    github_repo: str | None,
    allowed_repos: frozenset,
) -> list[dict]:
    """Extract actionable proposals from a COO decision memo.

    Returns a list of proposal dicts, one per actionable item found:

      type           : "proposal"
      title          : str        — GitHub issue title (≤72 chars)
      body           : str        — full GitHub issue body (markdown)
      repo           : str | None — pre-selected repo, or None when ambiguous
      repo_ambiguous : bool       — True when the operator must choose the repo
      allowed_repos  : list[str]  — sorted list for the UI dropdown
    """
    if not coo_output:
        return []

    proposals: list[dict] = []
    seen: set[str] = set()

    repo_ambiguous = not bool(github_repo)
    sorted_repos = sorted(allowed_repos)

    def _add(action_text: str) -> None:
        text = action_text.strip()
        if not text or len(text) < _MIN_ACTION_LEN:
            return
        key = text.lower()[:80]
        if key in seen:
            return
        seen.add(key)
        proposals.append({
            "type": "proposal",
            "title": text[:72],
            "body": build_proposal_issue_body(coo_output, text),
            "repo": github_repo if not repo_ambiguous else None,
            "repo_ambiguous": repo_ambiguous,
            "allowed_repos": sorted_repos,
        })

    # 1. Numbered next-action items (primary — trigger-happy source).
    block_match = _NEXT_ACTIONS_BLOCK_RE.search(coo_output)
    if block_match:
        for line in block_match.group(1).splitlines():
            m = _ACTION_LINE_RE.match(line)
            if m:
                _add(m.group(1))

    # 2. Explicit /copilot task sentences (secondary — legacy compatibility).
    for m in _COPILOT_TASK_RE.finditer(coo_output):
        task = m.group(1).strip()[:200]
        if task:
            _add(task)

    return proposals


def build_proposal_issue_body(coo_memo: str, action_text: str) -> str:
    """Build a GitHub issue body from the COO decision memo and action text."""
    memo_lines = (coo_memo or "").splitlines()
    memo_snippet = "\n".join(memo_lines[-50:])
    return (
        f"## Team Review Action\n\n"
        f"**Task:** {action_text}\n\n"
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
