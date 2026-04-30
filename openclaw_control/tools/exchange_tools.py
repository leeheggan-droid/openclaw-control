"""openclaw_control/tools/exchange_tools.py

Thin read-only wrappers around the Kraken and Alpaca REST APIs.

Used as a fallback by the trade-history agent tool when the local SQLite
trade log is empty (i.e. the trading bot has not been configured to POST
to /trades/log on this control panel).

Public API
----------
  fetch_kraken_trades(limit)          -> list[dict]  | str (error message)
  fetch_kraken_open_positions()       -> list[dict]  | str
  fetch_kraken_trade_balance()        -> dict        | str
  fetch_alpaca_trades(limit)          -> list[dict]  | str (error message)
  fetch_alpaca_open_positions()       -> list[dict]  | str
  fetch_alpaca_account()              -> dict        | str
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
_MIN_VOLUME_GUARD = 1e-12  # avoid division-by-zero when computing average entry price


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


# ---------------------------------------------------------------------------
# Kraken — positions & account
# ---------------------------------------------------------------------------


def fetch_kraken_open_positions() -> list[dict[str, Any]] | str:
    """Fetch currently open positions from the Kraken OpenPositions endpoint.

    Returns a list of normalised position dicts on success, or an error string.
    """
    api_key = settings.kraken_api_key
    api_secret = settings.kraken_api_secret

    if not api_key or not api_secret:
        return "KRAKEN_API_KEY / KRAKEN_API_SECRET not set in environment."

    url_path = "/0/private/OpenPositions"
    nonce = str(int(time.time() * 1000))
    data = {"nonce": nonce, "docalcs": "true"}

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

    positions_map: dict[str, Any] = payload.get("result", {})
    if not positions_map:
        return []

    results = []
    for pos_id, pos in positions_map.items():
        results.append(
            {
                "source": "kraken",
                "position_id": pos_id,
                "symbol": pos.get("pair", ""),
                "side": pos.get("type", ""),
                "size": float(pos.get("vol", 0)),
                "size_closed": float(pos.get("vol_closed", 0)),
                "entry_price": float(pos.get("cost", 0)) / max(float(pos.get("vol", 1)), _MIN_VOLUME_GUARD),
                "cost": float(pos.get("cost", 0)),
                "fee": float(pos.get("fee", 0)),
                "net_pnl": float(pos.get("net", 0)) if pos.get("net") is not None else None,
                "unrealised_pnl": float(pos.get("value", 0)) - float(pos.get("cost", 0)) if pos.get("value") is not None else None,
                "open_ts": _unix_to_iso(pos.get("time")),
                "margin": float(pos.get("margin", 0)),
                "status": pos.get("posstatus", "open"),
            }
        )
    return results


def fetch_kraken_trade_balance() -> dict[str, Any] | str:
    """Fetch the Kraken trade balance (equity, unrealised P&L, margin).

    Returns a dict on success, or an error string.
    """
    api_key = settings.kraken_api_key
    api_secret = settings.kraken_api_secret

    if not api_key or not api_secret:
        return "KRAKEN_API_KEY / KRAKEN_API_SECRET not set in environment."

    url_path = "/0/private/TradeBalance"
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

    result = payload.get("result", {})
    return {
        "equity": _safe_float(result.get("e")),         # trade balance + unrealised P&L
        "trade_balance": _safe_float(result.get("tb")), # trade balance (no open positions)
        "margin": _safe_float(result.get("m")),         # current initial margin
        "unrealised_pnl": _safe_float(result.get("n")), # unrealised net profit/loss
        "cost_basis": _safe_float(result.get("c")),     # current positions cost
        "valuation": _safe_float(result.get("v")),      # current floating valuation
        "free_margin": _safe_float(result.get("mf")),   # free margin
        "margin_level": _safe_float(result.get("ml")),  # margin level (%)
        "source": "kraken",
    }


# ---------------------------------------------------------------------------
# Alpaca — positions & account
# ---------------------------------------------------------------------------


def fetch_alpaca_open_positions() -> list[dict[str, Any]] | str:
    """Fetch currently open positions from the Alpaca Positions endpoint.

    Tries the live endpoint first; falls back to paper if live returns 403/401.

    Returns a list of normalised position dicts on success, or an error string.
    """
    api_key = settings.alpaca_api_key
    api_secret = settings.alpaca_api_secret

    if not api_key or not api_secret:
        return "ALPACA_API_KEY / ALPACA_API_SECRET not set in environment."

    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }

    for base_url in (_ALPACA_BASE, _ALPACA_PAPER_BASE):
        try:
            resp = requests.get(
                f"{base_url}/v2/positions",
                headers=headers,
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.status_code in (401, 403) and base_url == _ALPACA_BASE:
                continue
            resp.raise_for_status()
            positions: list[dict[str, Any]] = resp.json()
            break
        except requests.HTTPError as exc:
            return f"Alpaca API error ({base_url}): {exc}"
        except Exception as exc:
            return f"Alpaca API request failed: {exc}"
    else:
        return "Alpaca API: both live and paper endpoints rejected the credentials."

    if not isinstance(positions, list):
        return f"Alpaca API: unexpected response format: {str(positions)[:200]}"

    results = []
    for pos in positions:
        results.append(
            {
                "source": "alpaca",
                "symbol": pos.get("symbol", ""),
                "side": pos.get("side", ""),
                "size": float(pos.get("qty") or 0),
                "entry_price": float(pos.get("avg_entry_price") or 0),
                "current_price": float(pos.get("current_price") or 0),
                "market_value": float(pos.get("market_value") or 0),
                "cost_basis": float(pos.get("cost_basis") or 0),
                "unrealised_pnl": float(pos.get("unrealized_pl") or 0),
                "unrealised_pnl_pct": float(pos.get("unrealized_plpc") or 0),
                "realised_pnl": float(pos.get("realized_pl") or 0) if pos.get("realized_pl") is not None else None,
                "change_today": float(pos.get("change_today") or 0),
            }
        )
    return results


def fetch_alpaca_account() -> dict[str, Any] | str:
    """Fetch the Alpaca account summary (equity, buying power, cash, P&L).

    Tries the live endpoint first; falls back to paper if live returns 403/401.

    Returns a dict on success, or an error string.
    """
    api_key = settings.alpaca_api_key
    api_secret = settings.alpaca_api_secret

    if not api_key or not api_secret:
        return "ALPACA_API_KEY / ALPACA_API_SECRET not set in environment."

    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }

    for base_url in (_ALPACA_BASE, _ALPACA_PAPER_BASE):
        try:
            resp = requests.get(
                f"{base_url}/v2/account",
                headers=headers,
                timeout=_REQUEST_TIMEOUT,
            )
            if resp.status_code in (401, 403) and base_url == _ALPACA_BASE:
                continue
            resp.raise_for_status()
            acct: dict[str, Any] = resp.json()
            break
        except requests.HTTPError as exc:
            return f"Alpaca API error ({base_url}): {exc}"
        except Exception as exc:
            return f"Alpaca API request failed: {exc}"
    else:
        return "Alpaca API: both live and paper endpoints rejected the credentials."

    if not isinstance(acct, dict):
        return f"Alpaca API: unexpected response format: {str(acct)[:200]}"

    return {
        "source": "alpaca",
        "equity": _safe_float(acct.get("equity")),
        "cash": _safe_float(acct.get("cash")),
        "buying_power": _safe_float(acct.get("buying_power")),
        "portfolio_value": _safe_float(acct.get("portfolio_value")),
        "unrealised_pnl": _safe_float(acct.get("unrealized_pl")),
        "realised_pnl": _safe_float(acct.get("realized_pl")),
        "long_market_value": _safe_float(acct.get("long_market_value")),
        "short_market_value": _safe_float(acct.get("short_market_value")),
        "status": acct.get("status", ""),
        "account_number": acct.get("account_number", ""),
    }


# ---------------------------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------------------------


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
