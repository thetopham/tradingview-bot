# Simplify market state to local EMA slope and remove multi-source regimes

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This document must be maintained in accordance with PLANS.md located at repository root (`PLANS.md`).

## Purpose / Big Picture

The bot should determine market state locally from 5-minute Supabase OHLC data using an EMA slope and ATR normalization, removing reliance on multiple regime modules and n8n chart analysis. After this change, the webhook will produce BUY/SELL/HOLD decisions based on a single deterministic market state and execute via the simple strategy with server-side brackets. The dashboard should display this simplified state without multi-timeframe or hybrid regimes.

## Progress

- [x] (2025-01-10 00:00Z) Draft ExecPlan capturing goals and approach.
- [x] (2025-01-10 00:40Z) Implemented local market_state module with EMA/ATR slope and signal mapping.
- [x] (2025-01-10 00:40Z) Refactored api.py to compute market conditions from local 5m bars.
- [x] (2025-01-10 00:40Z) Simplified webhook decision path to rely on market_state and PositionManager gating.
- [x] (2025-01-10 00:40Z) Cleaned scheduler, strategies, and dashboard for 5m-only flow; removed legacy regime module.
- [ ] Validate bot start-up and dashboard render with new market state.
- [ ] Finalize documentation in this plan with outcomes and lessons.

## Surprises & Discoveries

- None yet.

## Decision Log

- Decision: Use EMA21 over 5m bars with ATR normalization and deadband classification (trending_up/down/sideways) as the single market state source.
  Rationale: Meets requirements for deterministic local computation and replaces prior hybrid regime complexity.
  Date/Author: 2025-01-10 / Codex

## Outcomes & Retrospective

(To be completed after implementation.)

## Context and Orientation

Key entrypoint is `tradingview_projectx_bot.py` exposing the Flask webhook and orchestrating AI/trading decisions. `api.py` handles Supabase and external calls. `strategies.py` holds order execution helpers including `run_simple`. Market regime helpers currently live in `market_regime.py` and variants; these will be replaced. `scheduler.py` schedules maintenance/regime updates. Dashboard UI is at `static/dashboard.html`. Position/risk gating occurs in `position_manager.py`.

## Plan of Work

1. Create or repurpose a single market state module (e.g., `market_state.py` or updated `market_regime.py`) that fetches 5m OHLC candles, computes EMA21 and ATR, derives slope over a lookback window, normalizes slope by ATR, applies a configurable deadband to classify trend (trending_up, trending_down, sideways), and returns a small dict with timestamp, trend, slope_norm, confidence based on absolute slope_norm, supporting_factors (if any), and a deterministic signal mapping (up->BUY, down->SELL, sideways->HOLD).
2. Update `api.py` `get_market_conditions_summary()` to pull recent 5m candles via existing Supabase helpers, call the market state computation locally, and return a structure compatible with dashboard/decision logic (including regime/trend/confidence/supporting_factors defaults). Remove reliance on `fetch_multi_timeframe_analysis` or n8n for regime.
3. Refactor `tradingview_projectx_bot.py` decision flow so webhook uses the new market state. Remove calls to `ai_trade_decision_with_regime` or hybrid regime gating. Allow optional AI decision only with a compact payload containing alert info, market_state, position context, and risk status. Enforce PositionManager `can_trade` and simple rule: sideways -> HOLD else follow signal.
4. Simplify scheduler in `scheduler.py` by removing jobs that fetch charts/update hybrid regimes or check good_regimes. Align any remaining regime labels to trending_up/trending_down/sideways.
5. Trim `strategies.py` to keep only `run_simple` for market orders (server-side brackets). Remove legacy strategy functions and imports tied to old regime logic. Ensure execution path references only `run_simple` and works with new signals.
6. Adjust `static/dashboard.html` to present only 5m market state/regime, tolerating missing optional fields. Update labels that mention multiple timeframes and ensure market regime card uses new fields.
7. Remove or move legacy regime modules (e.g., `market_regime_hybrid.py`, `market_regime_ohlc.py`) to a `legacy/` folder or delete them; clean imports throughout the codebase to avoid missing references.

## Concrete Steps

- Work from repository root `/workspace/tradingview-bot`.
- Implement market state module and adjust imports.
- Modify `api.py`, `tradingview_projectx_bot.py`, `scheduler.py`, `strategies.py`, and `static/dashboard.html` per plan.
- Delete or relocate legacy regime modules and update references.
- Run `python -m py_compile` on touched Python files or start the Flask app (`python tradingview_projectx_bot.py`) to ensure no runtime import errors.

## Validation and Acceptance

- Start Flask app via `python tradingview_projectx_bot.py`; ensure it boots without errors and logs show market state computed locally without n8n calls.
- Trigger webhook with sample payload (or inspect logs) to confirm decision uses local market_state signal (sideways -> HOLD, trending_up -> BUY, trending_down -> SELL) and executes via `run_simple` when allowed by PositionManager.
- Load `static/dashboard.html` in a browser; verify the market regime card shows 5m trend/confidence without crashing or referencing missing timeframes.

## Idempotence and Recovery

- Market state computation should handle missing/insufficient candles gracefully by returning defaults and HOLD signals; this keeps webhook and dashboard resilient.
- Removing legacy modules is safe if imports are cleaned; if issues arise, restore from version control.

## Artifacts and Notes

- Record key log snippets showing local market state calculation and absence of n8n regime calls after implementation.

## Interfaces and Dependencies

- Market state function should accept recent 5m OHLC data (list of dicts) and return a dict with keys: `timestamp`, `trend` (trending_up|trending_down|sideways), `confidence` (0-1), `slope_norm` (float), `supporting_factors` (list), and `signal` (BUY|SELL|HOLD).
- `api.get_market_conditions_summary()` should expose the market state in its response for dashboard and decision logic.
- `strategies.run_simple(symbol, signal, quantity, account_type, client)` remains the execution entry and should be the only strategy invoked.
