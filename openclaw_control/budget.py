"""Daily budget tracker for the shared $5/day OpenAI spend cap.

Spend is estimated from token usage using GPT-4o list pricing:
    Input:  $2.50 / 1 M tokens
    Output: $10.00 / 1 M tokens

These constants can be overridden via environment variables
OPENCLAW_INPUT_COST_PER_TOKEN and OPENCLAW_OUTPUT_COST_PER_TOKEN
(values are USD per single token).

Budget states
-------------
- *normal*  : daily_spent < LOW_THRESHOLD   — all agents run as configured.
- *low*     : LOW_THRESHOLD ≤ daily_spent < DAILY_LIMIT
              Main continues normally; P&L/Quant trim outputs; COO refuses.
- *exhausted*: daily_spent ≥ DAILY_LIMIT   — same behaviour as *low* (COO already
              refused, so specialists may still return short answers for Main to use).
"""

from __future__ import annotations

import os
import threading
from datetime import date

# ---------------------------------------------------------------------------
# Pricing (USD per token; defaults to GPT-4o list rates)
# ---------------------------------------------------------------------------
_INPUT_COST_PER_TOKEN: float = float(
    os.getenv("OPENCLAW_INPUT_COST_PER_TOKEN", str(2.50 / 1_000_000))
)
_OUTPUT_COST_PER_TOKEN: float = float(
    os.getenv("OPENCLAW_OUTPUT_COST_PER_TOKEN", str(10.00 / 1_000_000))
)

DAILY_LIMIT: float = 5.00   # USD
LOW_THRESHOLD: float = 4.00  # USD — "budget is low" once spend crosses this

# ---------------------------------------------------------------------------
# Internal state (thread-safe)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_day: date = date.today()
_spent: float = 0.0


def _maybe_reset() -> None:
    """Reset counters when the calendar day rolls over.  Must be called inside *_lock*."""
    global _day, _spent
    today = date.today()
    if today != _day:
        _day = today
        _spent = 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_usage(input_tokens: int, output_tokens: int) -> None:
    """Accumulate token counts and update the estimated daily spend.

    Should be called after every successful agent run.
    """
    cost = input_tokens * _INPUT_COST_PER_TOKEN + output_tokens * _OUTPUT_COST_PER_TOKEN
    global _spent
    with _lock:
        _maybe_reset()
        _spent += cost


def daily_spent() -> float:
    """Return the estimated USD spend so far today."""
    with _lock:
        _maybe_reset()
        return _spent


def is_low() -> bool:
    """True when daily spend has crossed LOW_THRESHOLD (≥ $4.00 by default)."""
    return daily_spent() >= LOW_THRESHOLD


def is_exhausted() -> bool:
    """True when daily spend has reached or exceeded DAILY_LIMIT (≥ $5.00 by default)."""
    return daily_spent() >= DAILY_LIMIT
