# Reduction-mode trigger refactor

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This plan must be maintained in accordance with PLANS.md located at ./PLANS.md.

## Purpose / Big Picture

Convert the bot into a lightweight trade trigger that relies on TopstepX Auto OCO brackets rather than local stop/target logic. The outcome is a simplified webhook flow that reads Supabase OHLCV data, derives EMA-based trend direction per timeframe, and issues at most one market entry order when risk gates allow. With TRADING_ENABLED=false by default, contributors can run the bot end-to-end to observe market state and planned actions without placing live orders.

## Progress

- [x] (2025-02-24 06:00Z) Drafted initial ExecPlan skeleton and purpose.
- [x] (2025-02-24 07:10Z) Implemented market_state builder with aggregation, EMA, and slope.
- [x] (2025-02-24 07:15Z) Implemented trigger engine and execution helpers.
- [x] (2025-02-24 07:30Z) Refactored tradingview_projectx_bot.py to use new trigger flow and remove bracket dispatch.
- [x] (2025-02-24 07:35Z) Simplified scheduler and SignalR usage for logging-only lifecycle.
- [ ] Run smoke checks with TRADING_ENABLED=false and document results.

## Surprises & Discoveries

- None yet.

## Decision Log

- Decision: Use a single ExecPlan file (execplan_reduction_arch.md) to cover the entire reduction refactor.  Rationale: keeps context centralized for multi-file changes.  Date/Author: 2025-02-24 / assistant.

## Outcomes & Retrospective

- Pending implementation.

## Context and Orientation

The Flask webhook entrypoint is tradingview_projectx_bot.py. Position state and gating utilities live in position_manager.py and state.py. Strategies and market regime helpers currently use multiple JSON configs and may involve screenshot workflows; these will be bypassed for the reduction MVP. Scheduler tasks in scheduler.py trigger image workflows and maintenance jobs. SignalR logging is handled in signalr_listener.py. Configuration defaults and environment flags are defined in config.py and env.example. New helper modules will be added beside existing top-level Python files.

## Plan of Work

1. Add a market_state.py module that fetches ~600 1m bars from Supabase tv_datafeed using existing api helpers, aggregates to 5m/15m/30m, computes EMA21 and normalized slopes, and returns a structured MarketState dictionary. Include logging and minimize API calls by batching fetches.
2. Add a trigger_engine.py module that inspects MarketState and position context from PositionManager.get_position_context_for_ai(). Determine BUY/SELL/HOLD based on EMA slopes with HOLD-first gating (flat window, account metrics, existing position). Provide reason codes for transparency.
3. Add an execution.py module with send_entry that issues a single market order via ProjectX API, honoring TRADING_ENABLED and DEFAULT_SIZE. No local stop/TP placements.
4. Refactor tradingview_projectx_bot.py to build market_state, gather position context, invoke trigger_engine.decide, and call execution.send_entry when appropriate. Remove calls to run_bracket/run_brackmod/run_pivot and local bracket management. Ensure TRADING_ENABLED defaults to false and logs are clear.
5. Simplify scheduler.py to remove n8n image workflows and optional periodic market_state snapshots or trigger evaluations. Keep minimal logging cadence.
6. Trim SignalR listener usage to logging only, removing ensure_stops_match_position and phantom sweep logic while retaining log_trade_results_to_supabase on close events. Delete or clearly mark unused legacy paths.
7. Update env.example/config defaults if new settings are added, and adjust documentation in README if necessary. Keep smoke testing instructions simple with TRADING_ENABLED=false.

## Concrete Steps

- Work from repository root /workspace/tradingview-bot.
- Implement modules in sequence: market_state.py, trigger_engine.py, execution.py.
- Update tradingview_projectx_bot.py to wire new flow and remove legacy calls.
- Update scheduler.py and signalr_listener.py to strip bracket-specific behaviors.
- Run targeted sanity checks: python -m market_state (if added) or python tradingview_projectx_bot.py with TRADING_ENABLED=false to confirm logs show market state and action plan.
- Document observations in this plan under Progress and Surprises & Discoveries.

## Validation and Acceptance

After changes, start the Flask app locally with TRADING_ENABLED=false (default). Trigger the webhook or invoke market_state builder directly to see logs containing aggregated EMA slopes, derived regime, action plan, and whether an order would be sent. No stop/target orders should be placed. SignalR logs should continue capturing closes and Supabase logging should function when positions close. Scheduler should no longer trigger image workflows.

## Idempotence and Recovery

New modules are additive and refactors remove unused code paths. Re-running the app with TRADING_ENABLED=false is safe. If Supabase fetch fails, log and return HOLD without placing orders. Avoid deleting files unless clearly unused; when removing references, keep behavior clear in logs. Git history provides rollback; commit frequently.

## Artifacts and Notes

- None yet.

## Interfaces and Dependencies

- market_state.py exposes build_market_state() returning a MarketState dict with per-timeframe EMA and slope metrics.
- trigger_engine.py exposes decide(market_state, position_context) -> ActionPlan dict with action and reason_code keys.
- execution.py exposes send_entry(action, acct_id, symbol, size=DEFAULT_SIZE) that calls ProjectX place_market without SL/TP.
- tradingview_projectx_bot.py orchestrates webhook processing using these modules with TRADING_ENABLED gating.
- scheduler.py may optionally import market_state to snapshot state; it should not invoke n8n screenshot workflows.
- signalr_listener.py retains log_trade_results_to_supabase and removes ensure_stops_match_position/phantom sweeps.

