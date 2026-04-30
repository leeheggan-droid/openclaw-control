# Agent run for Issue #88

## Issue title
**Implement and trigger a 2-year historical backtest** for both "moon" a

## Issue body
## Team Review Action

**Task:** **Implement and trigger a 2-year historical backtest** for both "moon" and "moon_momentum" models using the available candle data, ensuring results (P&L, Sharpe, win rate, and detailed per-trade attribution) are written to a persistent file or database.

## COO Decision Memo

```
**COO Decision Memo: Backtest and Comparative Analytics for “moon” and “moon_momentum” Models**

---

1. **What we know / What we don't know**

   **We know:**
   - The OpenClaw system currently holds only recent, live trading logs and summary stats for the "moon_momentum" model: 31 trades, win rate ≈ 26%, Sharpe ≈ -2.07, net P&L ≈ -7.2 (currency/scale not specified).
   - Price feeds confirm access to 2 years of historical crypto candle data from Kraken.
   - There is no historical backtest or stored attribution for either "moon" or "moon_momentum" for any period (2 years or last 3 weeks); no evidence that backtest functionality is currently active or outputs persisted.
   - No gross-vs-net P&L breakdown, nor any fee or slippage modeling, is observed in reports or logs.

   **We don’t know:**
   - The true performance of either model across 2 years or the most recent 3 weeks—i.e., no historical return, risk, or attribution data is present or accessible.
   - Whether the system can currently trigger, run, and persist a proper backtest (none has been detected in logs or as persisted output).
   - Fee, slippage, and turnover impacts on actual or simulated results.
   - If historic results would support or contradict recent weak live outcomes.

---

2. **Risks & Constraints**

   **Risks:**
   - No foundation for product, risk, or investment decisions if only a minimal (statistically weak and regime-bound) live sample is available.
   - Lack of historical P&L, Sharpe, or attribution in logs means audit, compliance, and investor reporting are compromised.
   - Danger of repeating recent poor strategy performance (Sharpe ≈ -2, win rate ≈ 26%) if no thorough multi-period review is run.
   - Regulatory/strategic blind spot: No persistent store for evidence or repeatability.

   **Constraints:**
   - All analytics and data extraction must rely on what the system logs on the target server (via SSH, non-destructive).
   - Do not disrupt live trading or clear live container logs.
   - Backtest triggers and data output are currently not observable; workflow and storage must be implemented or surfaced.
   - Realism requires fee/slippage modeling, not only raw returns.

---

3. **Next Actions (max 3)**

   1. **Implement and trigger a 2-year historical backtest** for both "moon" and "moon_momentum" models using the available candle data, ensuring results (P&L, Sharpe, win rate, and detailed per-trade attribution) are written to a persistent file or database.
   2. **Extract and generate comparative summary statistics** for the same models over both the 2-year and recent (last 3 weeks) periods, with explicit accounting for fees and slippage; present these side-by-side for review.
   3. **Ensure both summary and full per-trade attribution reports are stored and accessible** for quant, risk, and operations review—auditable, repeatable, and aligned with compliance best practices.

---

4. **Code Change Justification**

   - **Justified.** The inability to persist, expose, and compare historical vs. recent model performance is a critical operational blocker. Audit, quant review, and model oversight all require persisted, queryable backtest and attribution data for both models and all required timeframes.

   **/copilot task sentence:**  
   Implement a backtest routine for both "moon" and "moon_momentum" that, when invoked, runs over 2 years of historical candle data, saves P&L, Sharpe, win rate, and per-trade attribution to a persistent file/database, and
```

## Constraints
- No secrets or credentials added to source code
- No destructive operations introduced
- Changes limited to the minimum required
- Match existing code style and conventions

## Acceptance Criteria
- [ ] Task completed as described above
- [ ] Local test passed: `git pull; uvicorn web_app:app --reload;` then verified in browser


## Pre-approval report
## 1) Investigation Plan

1.  **SSH into the VPS:** Access the OpenClaw system's codebase.
2.  **Locate Model Definitions

---
Generated at 20260430-041124Z UTC
