"""trigger_happy_proposals.py

Operator-grade GitHub issue proposal extraction for Team Review next actions.

The system proposes a GitHub issue for EVERY actionable item found in the COO
decision memo.  The operator confirms (or cancels) each one — the system never
auto-creates issues without an explicit operator confirmation.

When the target repository is ambiguous (more than one repo matches the content
signals), proposal events include ``repo_ambiguous=True`` and the candidate
allowed_repos list so the UI can ask the operator to pick.

Public API
----------
proposal_fingerprint(title) -> str
    Return a 12-char hex fingerprint of a proposal title for 24h dedup.

extract_proposals(coo_output, github_repo, allowed_repos, seen_fps=None) -> list[dict]
    Parse the COO memo and return proposal dicts ready to emit as feed events.

build_proposal_issue_body(coo_memo, action_text) -> str
    Build a GitHub issue body from the COO memo and action text.
"""

from __future__ import annotations

import hashlib as _hashlib
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

# Number of hex characters in the proposal fingerprint.
_FINGERPRINT_LEN = 12


def proposal_fingerprint(title: str) -> str:
    """Return a short hex fingerprint for a proposal title (used for 24h dedup)."""
    return _hashlib.md5(title.strip().lower().encode()).hexdigest()[:_FINGERPRINT_LEN]


# Maximum length of the GitHub issue title (GitHub enforces 256 chars but
# shorter titles are more readable in list views).
_MAX_ISSUE_TITLE_LEN = 72

# Length of the lowercase deduplication key used to detect near-duplicate actions.
_DEDUP_KEY_LEN = 80


def extract_proposals(
    coo_output: str,
    github_repo: str | None,
    allowed_repos: frozenset,
    seen_fps: set[str] | None = None,
) -> list[dict]:
    """Extract actionable proposals from a COO decision memo.

    Returns a list of proposal dicts, one per actionable item found:

      type           : "proposal"
      title          : str        — GitHub issue title (≤72 chars)
      body           : str        — full GitHub issue body (markdown)
      repo           : str | None — pre-selected repo, or None when ambiguous
      repo_ambiguous : bool       — True when the operator must choose the repo
      allowed_repos  : list[str]  — sorted list for the UI dropdown
      fingerprint    : str        — 12-char hex used for 24h deduplication

    ``seen_fps`` is an optional set of fingerprints already emitted within the
    last 24 h; any proposal whose fingerprint is in this set is skipped.
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
        key = text.lower()[:_DEDUP_KEY_LEN]
        if key in seen:
            return
        fp = proposal_fingerprint(text)
        if seen_fps and fp in seen_fps:
            return
        seen.add(key)
        proposals.append({
            "type": "proposal",
            "title": text[:_MAX_ISSUE_TITLE_LEN],
            "body": build_proposal_issue_body(coo_output, text),
            "repo": github_repo if not repo_ambiguous else None,
            "repo_ambiguous": repo_ambiguous,
            "allowed_repos": sorted_repos,
            "fingerprint": fp,
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
