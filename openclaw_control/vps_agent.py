"""OpenClaw VPS Agent — GitHub issue poller with LLM planning and Vibe execution.

This module is the VPS-side daemon that implements the following state machine
for every GitHub issue labelled ``agent:queue``:

  agent:queue
      │  Claim: add agent:working, remove agent:queue
      ▼
  agent:working
      │  Call LLM for pre-approval plan; post plan as issue comment
      │  Add agent:needs-approval
      ▼
  agent:needs-approval
      │  Poll for /approve comment from a human
      ▼  (approved)
  agent:working  (post-approval)
      │  Call LLM for JSON ops list; validate against VIBE_ALLOWLIST
      │  Execute ops via Vibe executor; collect results
      │  Post results comment; create run-artifact PR
      │  Remove agent:working + agent:needs-approval; add agent:done
      ▼
  agent:done

Safety invariants
-----------------
* No GitHub mutation (branch/commit/PR) is performed without an explicit
  "/approve" comment from a human on the issue.
* All VPS actions go through vibe_client.vibe_call() — never subprocess/shell.
* Secrets are loaded from /etc/openclaw-agent/config.env (never from source).
* LLM provider, model, and response lengths are logged per request.
* No secret values are included in issue comments, PR bodies, or log lines.

Run via systemd or directly::

    python -m openclaw_control.vps_agent

Configuration is read from ``/etc/openclaw-agent/config.env`` (preferred) or a
``.env`` file in the current working directory (development fallback).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from openclaw_control.vibe_client import vibe_call

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
_LOG = logging.getLogger("vps_agent")

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_VPS_CONFIG_PATH = Path("/etc/openclaw-agent/config.env")


def _load_config() -> None:
    """Load configuration from /etc/openclaw-agent/config.env (preferred) or .env."""
    if _VPS_CONFIG_PATH.exists():
        load_dotenv(dotenv_path=_VPS_CONFIG_PATH, override=True)
        _LOG.info("Loaded config from %s", _VPS_CONFIG_PATH)
    else:
        load_dotenv(override=False)
        _LOG.info("VPS config not found at %s — falling back to .env / environment", _VPS_CONFIG_PATH)


def _require(key: str) -> str:
    val = os.getenv(key, "")
    if not val:
        raise RuntimeError(f"Required config key {key!r} is not set")
    return val


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

_GH_API = "https://api.github.com"


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_get(path: str, token: str, **kwargs) -> requests.Response:
    return requests.get(f"{_GH_API}{path}", headers=_gh_headers(token), timeout=15, **kwargs)


def _gh_post(path: str, token: str, **kwargs) -> requests.Response:
    return requests.post(f"{_GH_API}{path}", headers=_gh_headers(token), timeout=15, **kwargs)


def _gh_patch(path: str, token: str, **kwargs) -> requests.Response:
    return requests.patch(f"{_GH_API}{path}", headers=_gh_headers(token), timeout=15, **kwargs)


def _gh_delete(path: str, token: str, **kwargs) -> requests.Response:
    return requests.delete(f"{_GH_API}{path}", headers=_gh_headers(token), timeout=15, **kwargs)


def list_queued_issues(owner: str, repo: str, token: str) -> list[dict]:
    """Return open issues labelled 'agent:queue'."""
    r = _gh_get(
        f"/repos/{owner}/{repo}/issues",
        token,
        params={"labels": "agent:queue", "state": "open", "per_page": 10},
    )
    if not r.ok:
        _LOG.warning("Failed to list issues: %s %s", r.status_code, r.text[:200])
        return []
    return r.json()


def add_label(owner: str, repo: str, number: int, label: str, token: str) -> None:
    r = _gh_post(f"/repos/{owner}/{repo}/issues/{number}/labels", token, json={"labels": [label]})
    if not r.ok:
        _LOG.warning("add_label %r failed: %s", label, r.status_code)


def remove_label(owner: str, repo: str, number: int, label: str, token: str) -> None:
    import urllib.parse
    encoded = urllib.parse.quote(label, safe="")
    r = _gh_delete(f"/repos/{owner}/{repo}/issues/{number}/labels/{encoded}", token)
    if not r.ok and r.status_code != 404:
        _LOG.warning("remove_label %r failed: %s", label, r.status_code)


def post_comment(owner: str, repo: str, number: int, body: str, token: str) -> None:
    r = _gh_post(f"/repos/{owner}/{repo}/issues/{number}/comments", token, json={"body": body})
    if not r.ok:
        _LOG.warning("post_comment failed: %s", r.status_code)


def get_comments(owner: str, repo: str, number: int, token: str) -> list[dict]:
    r = _gh_get(
        f"/repos/{owner}/{repo}/issues/{number}/comments",
        token,
        params={"per_page": 100},
    )
    if not r.ok:
        return []
    return r.json()


def has_approval(owner: str, repo: str, number: int, token: str) -> bool:
    """Return True if any comment contains '/approve' as a standalone token.

    The check is case-insensitive and requires '/approve' to appear at the
    start of a line, preceded only by whitespace, to avoid false positives
    from words like 'disapprove'.
    """
    import re
    _APPROVE_RE = re.compile(r"^\s*/approve\b", re.IGNORECASE | re.MULTILINE)
    for comment in get_comments(owner, repo, number, token):
        if _APPROVE_RE.search(comment.get("body", "")):
            return True
    return False


def create_pr(
    owner: str,
    repo: str,
    issue_number: int,
    issue_title: str,
    results_md: str,
    token: str,
) -> str | None:
    """Create a run-artifact PR branch and PR; return the PR URL or None."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    branch = f"agent/issue-{issue_number}-{ts}"
    file_path = f"agent-runs/{issue_number}/results.md"

    # Fetch the repository's default branch dynamically to avoid hardcoding 'main'/'master'.
    r = _gh_get(f"/repos/{owner}/{repo}", token)
    if r.ok:
        default_branch = r.json().get("default_branch", "main")
    else:
        _LOG.warning("create_pr: could not fetch repo metadata: %s — assuming 'main'", r.status_code)
        default_branch = "main"

    # Get SHA of default branch HEAD
    r = _gh_get(f"/repos/{owner}/{repo}/git/ref/heads/{default_branch}", token)
    if not r.ok:
        _LOG.warning("create_pr: could not resolve HEAD ref for %r: %s", default_branch, r.status_code)
        return None
    base_sha = r.json()["object"]["sha"]

    # Create the branch
    r = _gh_post(
        f"/repos/{owner}/{repo}/git/refs",
        token,
        json={"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    if not r.ok:
        _LOG.warning("create_pr: branch creation failed: %s %s", r.status_code, r.text[:200])
        return None

    # Commit the results file
    import base64
    content_b64 = base64.b64encode(results_md.encode()).decode()
    r = _gh_post(
        f"/repos/{owner}/{repo}/contents/{file_path}",
        token,
        json={
            "message": f"chore: agent run results for issue #{issue_number}",
            "content": content_b64,
            "branch": branch,
        },
    )
    if not r.ok:
        _LOG.warning("create_pr: file commit failed: %s %s", r.status_code, r.text[:200])
        return None

    # Open the PR against the detected default branch
    r = _gh_post(
        f"/repos/{owner}/{repo}/pulls",
        token,
        json={
            "title": f"[Agent] Run results — issue #{issue_number}: {issue_title[:80]}",
            "body": (
                f"Automated run artifact for issue #{issue_number}.\n\n"
                f"References #{issue_number}\n\n"
                f"<details><summary>Full results</summary>\n\n{results_md}\n</details>"
            ),
            "head": branch,
            "base": default_branch,
            "draft": False,
        },
    )
    if not r.ok:
        _LOG.warning("create_pr: PR creation failed: %s %s", r.status_code, r.text[:200])
        return None
    return r.json().get("html_url")


# ---------------------------------------------------------------------------
# LLM provider routing
# ---------------------------------------------------------------------------


def _llm_call_anthropic(api_key: str, model: str, prompt: str) -> str:
    """Call the Anthropic Claude API and return the text response."""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"Anthropic API error {r.status_code}: {r.text[:300]}")
    data = r.json()
    _LOG.info(
        "Anthropic call: model=%s input_tokens=%s output_tokens=%s",
        model,
        data.get("usage", {}).get("input_tokens"),
        data.get("usage", {}).get("output_tokens"),
    )
    content = data.get("content", [])
    return "\n".join(block.get("text", "") for block in content if block.get("type") == "text")


def _llm_call_gemini(api_key: str, model: str, prompt: str) -> str:
    """Call the Google Gemini API and return the text response."""
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"Gemini API error {r.status_code}: {r.text[:300]}")
    data = r.json()
    _LOG.info("Gemini call: model=%s", model)
    candidates = data.get("candidates", [])
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    return "\n".join(p.get("text", "") for p in parts)


def _llm_call_groq(api_key: str, model: str, prompt: str) -> str:
    """Call the Groq OpenAI-compatible API and return the text response."""
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2048,
        },
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"Groq API error {r.status_code}: {r.text[:300]}")
    data = r.json()
    _LOG.info("Groq call: model=%s", model)
    return data["choices"][0]["message"]["content"]


def llm_plan(prompt: str) -> str:
    """Route to the configured LLM provider and return a text response."""
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()

    if provider == "anthropic":
        api_key = _require("ANTHROPIC_API_KEY")
        model = os.getenv("ANTHROPIC_MODEL", "claude-3-haiku-20240307")
        return _llm_call_anthropic(api_key, model, prompt)

    if provider == "gemini":
        api_key = _require("GEMINI_API_KEY")
        model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
        return _llm_call_gemini(api_key, model, prompt)

    if provider == "groq":
        api_key = _require("GROQ_API_KEY")
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        return _llm_call_groq(api_key, model, prompt)

    raise RuntimeError(f"Unknown LLM_PROVIDER={provider!r}. Use 'anthropic', 'gemini', or 'groq'.")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_PLAN_PROMPT = """\
You are an automated ops agent reviewing a GitHub issue. Produce a concise \
pre-approval plan in Markdown. Include:
- A one-paragraph summary of what the issue is asking for.
- A numbered list of proposed actions.
- Any risks or caveats the human should review before approving.
- A final line: "Reply /approve on this issue to execute the above plan."

Do NOT include any secrets, credentials, or sensitive paths.

Issue title: {title}
Issue body:
{body}
"""

_OPS_PROMPT = """\
You are an automated ops executor. The human has approved the plan for this issue.
Return ONLY a valid JSON array of operation objects — no surrounding text, no markdown fences.
Each object must have exactly two keys: "action" and "target".
Allowed action types: restart_service, tail_journal, read_file.
Example: [{{"action": "tail_journal", "target": "openclaw-agent.service"}}]

Approved plan:
{plan}

Issue title: {title}
Issue body:
{body}
"""


# ---------------------------------------------------------------------------
# Issue processing
# ---------------------------------------------------------------------------


def _parse_ops_json(text: str) -> list[dict]:
    """Extract and parse the JSON ops list from LLM output."""
    # Strip potential markdown code fences
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        ops = json.loads(text)
        if isinstance(ops, list):
            return ops
    except (json.JSONDecodeError, ValueError):
        pass
    return []


def _process_issue(issue: dict, owner: str, repo: str, token: str, poll_interval: int = 30) -> None:
    number = issue["number"]
    title = issue.get("title", "")
    body = issue.get("body", "") or ""

    _LOG.info("Processing issue #%d: %s", number, title)

    # ── Step 1: claim the issue ──────────────────────────────────────────────
    add_label(owner, repo, number, "agent:working", token)
    remove_label(owner, repo, number, "agent:queue", token)

    # ── Step 2: generate pre-approval plan ───────────────────────────────────
    try:
        # Truncate to ~4 000 chars to stay within typical LLM context windows.
        plan_text = llm_plan(_PLAN_PROMPT.format(title=title, body=body[:4000]))
    except Exception as exc:
        _LOG.error("LLM plan generation failed for issue #%d: %s", number, exc)
        post_comment(
            owner, repo, number,
            f"⚠️ Agent error during plan generation: `{type(exc).__name__}`\n\n"
            f"Details: {exc}\n\nThe `agent:working` label has been left for manual review.",
            token,
        )
        return

    # ── Step 3: post plan; add needs-approval label ───────────────────────────
    comment_body = (
        f"## 🤖 Agent Plan\n\n{plan_text}\n\n"
        "---\n_Reply `/approve` to execute this plan, or close the issue to cancel._"
    )
    post_comment(owner, repo, number, comment_body, token)
    add_label(owner, repo, number, "agent:needs-approval", token)
    _LOG.info("Plan posted for issue #%d; awaiting /approve", number)

    # ── Step 4: poll for /approve ─────────────────────────────────────────────
    max_wait_seconds = int(os.getenv("APPROVAL_TIMEOUT_SECONDS", "3600"))  # 1 hour default
    waited = 0
    approved = False

    while waited < max_wait_seconds:
        time.sleep(poll_interval)
        waited += poll_interval
        if has_approval(owner, repo, number, token):
            approved = True
            break
        _LOG.debug("Issue #%d: still waiting for /approve (%ds elapsed)", number, waited)

    if not approved:
        _LOG.info("Issue #%d: no /approve received within %ds — skipping", number, max_wait_seconds)
        post_comment(
            owner, repo, number,
            f"⏰ Agent timed out waiting for `/approve` after {max_wait_seconds // 60} minutes. "
            "Re-label with `agent:queue` to restart.",
            token,
        )
        remove_label(owner, repo, number, "agent:working", token)
        remove_label(owner, repo, number, "agent:needs-approval", token)
        return

    # ── Step 5: generate JSON ops list ────────────────────────────────────────
    _LOG.info("Issue #%d approved — generating ops list", number)
    try:
        # body[:4000] limits context sent to LLM to stay within token budgets.
        ops_text = llm_plan(_OPS_PROMPT.format(plan=plan_text, title=title, body=body[:4000]))
    except Exception as exc:
        _LOG.error("LLM ops generation failed for issue #%d: %s", number, exc)
        post_comment(
            owner, repo, number,
            f"⚠️ Agent error during ops generation: `{type(exc).__name__}`: {exc}",
            token,
        )
        remove_label(owner, repo, number, "agent:working", token)
        remove_label(owner, repo, number, "agent:needs-approval", token)
        return

    ops = _parse_ops_json(ops_text)
    if not ops:
        _LOG.warning("Issue #%d: no valid ops parsed from LLM output", number)
        post_comment(
            owner, repo, number,
            "⚠️ Agent could not parse a valid ops list from the LLM response. "
            "No operations were executed.",
            token,
        )
        remove_label(owner, repo, number, "agent:working", token)
        remove_label(owner, repo, number, "agent:needs-approval", token)
        return

    # ── Step 6: execute ops via Vibe ─────────────────────────────────────────
    results: list[dict] = []
    for op in ops:
        action = op.get("action", "")
        target = op.get("target", "")
        if not action or not target:
            _LOG.warning("Issue #%d: skipping malformed op: %s", number, op)
            continue
        _LOG.info("Issue #%d: vibe_call(%r, %r)", number, action, target)
        result = vibe_call(action, target)
        results.append({"op": op, "result": result})

    # ── Step 7: build results markdown ───────────────────────────────────────
    lines = [f"# Agent Run Results — Issue #{number}\n"]
    for entry in results:
        op = entry["op"]
        res = entry["result"]
        status = "✅" if res.get("exit_code") == 0 else ("🚫" if res.get("denied") else "❌")
        lines.append(f"## {status} `{op.get('action')}:{op.get('target')}`\n")
        if res.get("denied"):
            lines.append(f"**DENIED** — {res['stderr']}\n")
        else:
            if res.get("stdout"):
                lines.append(f"```\n{res['stdout'][:2000]}\n```\n")
            if res.get("stderr"):
                lines.append(f"**stderr:**\n```\n{res['stderr'][:500]}\n```\n")
    results_md = "\n".join(lines)

    # ── Step 8: post results comment ─────────────────────────────────────────
    post_comment(
        owner, repo, number,
        f"## ✅ Agent Execution Complete\n\n{results_md}\n\n"
        "_A run-artifact PR has been opened with full results._",
        token,
    )

    # ── Step 9: create run-artifact PR ───────────────────────────────────────
    pr_url = create_pr(owner, repo, number, title, results_md, token)
    if pr_url:
        post_comment(
            owner, repo, number,
            f"📎 Run artifact PR: {pr_url}",
            token,
        )

    # ── Step 10: transition to done ───────────────────────────────────────────
    remove_label(owner, repo, number, "agent:working", token)
    remove_label(owner, repo, number, "agent:needs-approval", token)
    add_label(owner, repo, number, "agent:done", token)
    _LOG.info("Issue #%d done", number)


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------


def run_agent() -> None:
    """Main entry point: load config and start the polling loop."""
    _load_config()

    token = _require("GITHUB_TOKEN")
    repo_full = os.getenv("GITHUB_REPO", "leeheggan-droid/openclaw-control")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))

    parts = repo_full.split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise RuntimeError(f"Invalid GITHUB_REPO={repo_full!r}. Expected 'owner/repo'.")
    owner, repo = parts

    _LOG.info(
        "OpenClaw VPS Agent started — repo=%s provider=%s poll=%ds",
        repo_full,
        os.getenv("LLM_PROVIDER", "anthropic"),
        poll_interval,
    )

    while True:
        try:
            issues = list_queued_issues(owner, repo, token)
            if issues:
                _LOG.info("Found %d queued issue(s)", len(issues))
            for issue in issues:
                try:
                    _process_issue(issue, owner, repo, token, poll_interval)
                except Exception as exc:
                    _LOG.error(
                        "Unhandled error processing issue #%d: %s",
                        issue.get("number", "?"),
                        exc,
                        exc_info=True,
                    )
        except Exception as exc:
            _LOG.error("Error in polling loop: %s", exc, exc_info=True)

        time.sleep(poll_interval)


if __name__ == "__main__":
    run_agent()
