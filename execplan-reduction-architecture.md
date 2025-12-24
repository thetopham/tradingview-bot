# Reduction architecture for TradingView ProjectX bot

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. Follow PLANS.md (at /workspace/tradingview-bot/PLANS.md) for format and maintenance expectations.

## Purpose / Big Picture

Refactor the bot into a simplified trade-trigger architecture that reads OHLC data from Supabase, computes EMA slopes on aggregated timeframes, and places a single market order when allowed. The system should stop managing stops locally; TopstepX handles brackets. Webhooks and scheduler paths will evaluate signals using deterministic slope rules, logging actions and simulating when trading is disabled. After completion, running the webhook or scheduler will compute market state and either log HOLD or submit one market order respecting get-flat and account gates.

## Progress

- [x] (2025-02-25 12:20Z) Draft initial ExecPlan with scope, context, and tasks.
- [x] (2025-02-25 13:20Z) Implemented market_state.py and slope calculations.
- [x] (2025-02-25 13:25Z) Added trigger_engine and execution modules for gated decision/entry.
- [x] (2025-02-25 13:40Z) Wired tradingview_projectx_bot to reduction pipeline with logging and new env vars.
- [x] (2025-02-25 13:55Z) Simplified scheduler and SignalR listener to logging-only behavior.
- [x] (2025-02-25 14:05Z) Validation: ran py_compile smoke check; simulated mode remains default.
- [ ] Finalize retrospective and ensure Env/example updates.

## Surprises & Discoveries

- None yet.

## Decision Log

- Decision: Use new modules (market_state.py, trigger_engine.py, execution.py) to isolate responsibilities for data prep, decision, and execution.
  Rationale: Matches reduction architecture and keeps webhook/scheduler minimal.
  Date/Author: 2025-02-25 / assistant

## Outcomes & Retrospective

(To be completed after implementation.)

## Context and Orientation

The root Flask webhook entry is `tradingview_projectx_bot.py`, orchestrating auth, regime checks, strategies, and order routing. Supporting modules include `api.py` (ProjectX and Supabase calls), `position_manager.py` (position context and account metrics), `scheduler.py` (cron tasks), and `signalr_listener.py` (real-time updates). Configuration lives in `config.py` with environment defaults mirrored in `env.example`. Logging is configured via `logging_config.py`.

Legacy strategy helpers reside in `strategies.py` and `market_regime*.py`, which will be bypassed for the reduction path. Existing SignalR logic performs stop synchronization and cleanup; the new architecture must disable mutations and keep only logging of close events. Supabase table `tv_datafeed` stores 1m OHLCV rows; we will query ~600 recent rows for symbol MES and aggregate to 5m/15m/30m bars before calculating EMA21 and normalized slopes.

## Plan of Work

1. Add configuration defaults and env variables for TRADING_ENABLED, DEFAULT_SIZE, SLOPE_LOOKBACK, SLOPE_THRESHOLD, and MARKET_SYMBOL in `config.py` and `env.example`. Ensure safe defaults (trading disabled by default) and expose in code.
2. Implement `market_state.py` with `build_market_state(supabase_client, symbol)` that fetches recent 1m rows from Supabase, orders oldest to newest, aggregates to 5m/15m/30m OHLCV, computes EMA21 per timeframe, derives normalized slopes via linear regression over the last SLOPE_LOOKBACK EMA values divided by current price, and returns a structured market state with regime and reason.
3. Implement `trigger_engine.py` with `decide` that applies gating (get-flat, can_trade, has_position) and slope thresholds to produce BUY/SELL/HOLD with reason codes and slope detail metadata.
4. Implement `execution.py` with `send_entry` performing a single market order via `api.place_market` when trading is enabled; otherwise simulate and log.
5. Refactor `tradingview_projectx_bot.py` webhook handling to use the reduction pipeline: validate secret/account, handle explicit FLAT with `flatten_contract`, compute get-flat, build market state, fetch position context, decide action, and optionally send entry. Remove legacy strategy dispatch and AI/n8n flows. Add logging line summarizing regime, slopes, action, reason, TRADING_ENABLED, and account; log simulated orders when disabled.
6. Simplify `scheduler.py` to drop chart-image/n8n triggers and stop/TP management. New cron job should build market state and optionally evaluate triggers (without spamming webhooks), while keeping get-flat checks if needed. Ensure no stop placement or legacy loops remain.
7. Update `signalr_listener.py` to logging-only: keep connection/subscriptions and close logging to Supabase but remove stop synchronization, sweeps, or order mutations. Mark legacy functions as unused in MVP.
8. Quarantine or clearly mark deprecated functions in `strategies.py` and `api.py` so they are not invoked by the new flow, while preserving core API functions (place_market, get_contract, search_pos).
9. Validate by running available lightweight commands (e.g., `python -m py_compile ...` or server startup), exercising webhook flow in simulated mode, and updating the plan sections with progress, surprises, and retrospective.

## Concrete Steps

Run commands from `/workspace/tradingview-bot`.

- After coding, run quick syntax checks such as `python -m py_compile tradingview_projectx_bot.py market_state.py trigger_engine.py execution.py scheduler.py signalr_listener.py` to ensure no syntax errors.
- Optionally start the Flask app with `python tradingview_projectx_bot.py` to verify it boots with TRADING_ENABLED=false; observe logs for reduction pipeline messages when posting a sample payload.
- Use `git status` to monitor changes and commit with an imperative message once validation passes.

## Validation and Acceptance

Acceptance criteria:
- Webhook path computes market state from Supabase 1m data, determines regime based on normalized slopes, respects get-flat/account gates, logs the evaluation, and either simulates or submits a single market order without placing stops/TPs locally.
- Scheduler no longer triggers chart-image/n8n workflows or stop management; it only builds/logs market state and optional evaluation.
- SignalR listener performs no stop/order mutations and only logs close events to Supabase.
- New environment variables exist in `config.py` and `env.example` with safe defaults.
- All touched modules are free of syntax errors and logging includes simulated order messaging when trading is disabled.

## Idempotence and Recovery

Changes are additive and refactor-focused; re-running commands or restarting the app should be safe because trading is disabled by default. If Supabase fetch fails, functions should handle empty data by returning unknown regime/HOLD without sending orders. No migrations or destructive operations are involved.

## Artifacts and Notes

Keep log snippets showing decision output (regime, slopes, action, reason) for reference. If trading is disabled, logs should show "SIMULATED ORDER" with the planned action.

## Interfaces and Dependencies

- `market_state.build_market_state(supabase_client, symbol="MES") -> dict` returns market state with EMA and slope data aggregated from tv_datafeed 1m rows.
- `trigger_engine.decide(market_state, position_context, in_get_flat, trading_enabled) -> dict` returns action plan with reason codes and slope details.
- `execution.send_entry(action, acct_id, symbol, size, trading_enabled) -> dict` sends or simulates a market order using `api.place_market` side mapping BUY=0/SELL=1.
- Webhook depends on `PositionManager.get_position_context_for_ai`, `auth.in_get_flat`, and `api.get_contract` for contract IDs; trading-enabled flag gates calls.
