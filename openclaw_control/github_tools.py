"""Shared GitHub API helpers used by both web_app.py and service.py.

Centralised here to avoid duplicating the requests logic and to prevent
circular imports (web_app → service → web_app).
"""

from __future__ import annotations

import requests as _requests

from openclaw_control.config import settings

ALLOWED_REPOS = frozenset({
    "leeheggan-droid/openclaw-crypto",
    "leeheggan-droid/alpaca_orb_bite_bot",
    "leeheggan-droid/LinkedIn_Data_Centre_News",
    "leeheggan-droid/openclaw-control",
})


def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def try_assign_copilot(owner: str, repo: str, issue_number: int, token: str) -> str:
    """Attempt to assign Copilot to an issue. Returns 'assigned' or 'manual_required'."""
    headers = gh_headers(token)
    copilot_login = None
    try:
        r = _requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/assignees",
            headers=headers,
            timeout=10,
        )
        if r.ok:
            for user in r.json():
                login = user.get("login", "")
                if "copilot" in login.lower():
                    copilot_login = login
                    break
    except Exception:
        pass

    if not copilot_login:
        return "manual_required"

    try:
        r = _requests.post(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/assignees",
            json={"assignees": [copilot_login]},
            headers=headers,
            timeout=10,
        )
        if r.ok:
            return "assigned"
    except Exception:
        pass

    return "manual_required"


def create_github_issue(
    title: str,
    body: str,
    repo_full: str,
    labels: list[str] | None = None,
    token: str | None = None,
    assign_copilot: bool = False,
) -> dict | None:
    """Create a GitHub issue and optionally assign Copilot.

    ``repo_full`` must be in ALLOWED_REPOS; falls back to ``settings.github_repo``
    otherwise. Returns ``{issue_url, issue_number, used_repo, assignment}`` on
    success, or ``None`` when the token is absent or the API call fails.
    """
    token = token or settings.github_token
    if not token:
        return None

    if repo_full not in ALLOWED_REPOS:
        repo_full = settings.github_repo or "leeheggan-droid/openclaw-control"

    parts = repo_full.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    owner, repo = parts

    payload: dict = {"title": title, "body": body, "labels": labels or []}
    try:
        r = _requests.post(
            f"https://api.github.com/repos/{owner}/{repo}/issues",
            json=payload,
            headers=gh_headers(token),
            timeout=15,
        )
        if not r.ok:
            return None
        data = r.json()
        issue_number = data["number"]
        issue_url = data["html_url"]
    except Exception:
        return None

    assignment = "not_attempted"
    if assign_copilot:
        assignment = try_assign_copilot(owner, repo, issue_number, token)

    return {
        "issue_url": issue_url,
        "issue_number": issue_number,
        "used_repo": repo_full,
        "assignment": assignment,
    }
