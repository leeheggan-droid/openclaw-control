"""openclaw_control/tools/exchange_tools.py

Thin read-only wrappers around the Kraken and Alpaca REST APIs.

Used as a fallback by the trade-history agent tool when the local SQLite
trade log is empty (i.e. the trading bot has not been configured to POST
to /trades/log on this control panel).

Public API
----------
  fetch_kraken_trades(limit)   -> list[dict]  | str (error message)
  fetch_alpaca_trades(limit)   -> list[dict]  | str (error message)
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Any

import requests

from openclaw_control.config import settings

_logger = logging.getLogger(__name__)

_KRAKEN_BASE = "https://api.kraken.com"
_ALPACA_BASE = "https://api.alpaca.markets"
_ALPACA_PAPER_BASE = "https://paper-api.alpaca.markets"

_REQUEST_TIMEOUT = 10  # seconds
_ALPACA_MAX_LIMIT = 50


# ---------------------------------------------------------------------------
# Kraken
# ---------------------------------------------------------------------------

def _kraken_sign(url_path: str, data: dict[str, str], secret: str) -> str:
    """Return the API-Sign value for a Kraken private request."""
    postdata = urllib.parse.urlencode(data)
    encoded = (data["nonce"] + postdata).encode()
    message = url_path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def fetch_kraken_trades(limit: int = 5) -> list[dict[str, Any]] | str:
    """Fetch the most recent filled trades from the Kraken TradesHistory endpoint.

    Returns a list of normalised trade dicts on success, or an error string.
    """
    api_key = settings.kraken_api_key
    api_secret = settings.kraken_api_secret

    if not api_key or not api_secret:
        return "KRAKEN_API_KEY / KRAKEN_API_SECRET not set in environment."

    url_path = "/0/private/TradesHistory"
    nonce = str(int(time.time() * 1000))
    data = {"nonce": nonce}

    try:
        sign = _kraken_sign(url_path, data, api_secret)
    except Exception as exc:
        return f"Kraken signature error: {exc}"

    try:
        resp = requests.post(
            f"{_KRAKEN_BASE}{url_path}",
            headers={"API-Key": api_key, "API-Sign": sign},
            data=data,
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        return f"Kraken API request failed: {exc}"

    errors = payload.get("error", [])
    if errors:
        return f"Kraken API error: {', '.join(errors)}"

    trades_map: dict[str, Any] = payload.get("result", {}).get("trades", {})
    if not trades_map:
        return []

    # Sort by time descending and take the most recent `limit` trades.
    sorted_trades = sorted(trades_map.values(), key=lambda t: float(t.get("time", 0)), reverse=True)
    results = []
    for t in sorted_trades[:limit]:
        results.append(
            {
                "source": "kraken",
                "ts": _unix_to_iso(t.get("time")),
                "symbol": t.get("pair", ""),
                "side": t.get("type", ""),        # "buy" or "sell"
                "size": float(t.get("vol", 0)),
                "fill_price": float(t.get("price", 0)),
                "trade_id": t.get("ordertxid", ""),
                "fee": float(t.get("fee", 0)),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Alpaca
# ---------------------------------------------------------------------------

def fetch_alpaca_trades(limit: int = 5) -> list[dict[str, Any]] | str:
    """Fetch the most recent filled orders from the Alpaca Orders endpoint.

    Tries the live endpoint first; falls back to the paper endpoint if live
    returns a 403/401 (paper-only account).

    Returns a list of normalised trade dicts on success, or an error string.
    """
    api_key = settings.alpaca_api_key
    api_secret = settings.alpaca_api_secret

    if not api_key or not api_secret:
        return "ALPACA_API_KEY / ALPACA_API_SECRET not set in environment."

    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    params = {
        "status": "filled",
        "limit": min(max(1, limit), _ALPACA_MAX_LIMIT),
        "direction": "desc",
    }

    for base_url in (_ALPACA_BASE, _ALPACA_PAPER_BASE):
        try:
            resp = requests.get(
                f"{base_url}/v2/orders",
                headers=headers,
                params=params,
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.status_code in (401, 403) and base_url == _ALPACA_BASE:
                # Might be a paper-only key — try paper endpoint.
                continue
            resp.raise_for_status()
            orders: list[dict[str, Any]] = resp.json()
            break
        except requests.HTTPError as exc:
            return f"Alpaca API error ({base_url}): {exc}"
        except Exception as exc:
            return f"Alpaca API request failed: {exc}"
    else:
        return "Alpaca API: both live and paper endpoints rejected the credentials."

    if not isinstance(orders, list):
        return f"Alpaca API: unexpected response format: {str(orders)[:200]}"

    results = []
    for o in orders:
        filled_at = o.get("filled_at") or o.get("updated_at") or ""
        qty = float(o.get("filled_qty") or o.get("qty") or 0)
        avg_price = float(o.get("filled_avg_price") or 0)
        results.append(
            {
                "source": "alpaca",
                "ts": filled_at,
                "symbol": o.get("symbol", ""),
                "side": o.get("side", ""),
                "size": qty,
                "fill_price": avg_price,
                "trade_id": o.get("id", ""),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unix_to_iso(ts: Any) -> str:
    """Convert a Unix timestamp (int/float/str) to an ISO-8601 UTC string."""
    if ts is None:
        return ""
    try:
        return (
            datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
        )
    except Exception:
        return str(ts)
