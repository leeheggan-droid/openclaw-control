"""openclaw_control/tools/web_search.py

Brave Search API function_tool for the Main agent.
Returns the top N web results as plain text.
Falls back gracefully when the API key is missing or the request fails.
"""

from __future__ import annotations

import os

import requests as _requests
from agents import function_tool

_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_TIMEOUT = 10  # seconds
_RESULT_COUNT = 5


@function_tool
def web_search(query: str) -> str:
    """Search the web using Brave Search for current news, prices, or external information.

    Use this for:
    - Live crypto or stock prices / news
    - Exchange status pages
    - Documentation lookup
    - Anything requiring up-to-date information not in the workspace

    Returns the top search results as plain text.
    """
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        return (
            "❌ BRAVE_API_KEY is not configured. "
            "Set BRAVE_API_KEY in your .env file to enable web search."
        )

    query = (query or "").strip()
    if not query:
        return "❌ Empty search query."

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }
    params = {"q": query, "count": _RESULT_COUNT}

    try:
        resp = _requests.get(
            _BRAVE_ENDPOINT,
            headers=headers,
            params=params,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except _requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        return f"❌ Brave Search API error (HTTP {status}). Check your API key and quota."
    except _requests.RequestException as exc:
        return f"❌ Network error calling Brave Search: {type(exc).__name__}."
    except (KeyError, ValueError) as exc:
        return f"❌ Failed to parse Brave Search response: {type(exc).__name__} — {exc}."

    results = (data.get("web") or {}).get("results") or []
    if not results:
        return f"No web results found for: {query}"

    lines: list[str] = [f"Web search results for: {query}\n"]
    for i, r in enumerate(results[:_RESULT_COUNT], 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        desc = (r.get("description") or "").strip()
        lines.append(f"{i}. {title}")
        if url:
            lines.append(f"   {url}")
        if desc:
            lines.append(f"   {desc}")
        lines.append("")

    return "\n".join(lines).strip()
