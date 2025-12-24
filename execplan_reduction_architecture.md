# Reduce bot to trigger-only architecture

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This document must be maintained in accordance with PLANS.md located at `PLANS.md` in the repository root.

## Purpose / Big Picture

Refactor the TradingView ProjectX bot into a trigger-only execution engine that relies on TopstepX server-side Auto OCO brackets. The bot should read OHLCV data from Supabase tv_datafeed, aggregate timeframes, compute EMA21 slopes, and emit a simple BUY/SELL/HOLD decision without managing local stop loss or take profit orders. The end-to-end flow should log market state, action plans, and order placement while keeping trading disabled by default for safe validation.

## Progress

- [x] (2025-01-08 00:45Z) Drafted initial ExecPlan and outlined tasks.
- [x] (2025-01-08 01:10Z) Implemented market_state module for aggregation and EMA slope calculations.
- [x] (2025-01-08 01:12Z) Implemented trigger engine for HOLD-first gating and action selection.
- [x] (2025-01-08 01:14Z) Implemented execution helper for market-only entry placement.
- [x] (2025-01-08 01:20Z) Refactored tradingview_projectx_bot orchestration to use new trigger architecture.
- [x] (2025-01-08 01:25Z) Simplified scheduler and SignalR usage, removing local bracket handling.
- [ ] Validate flow with TRADING_ENABLED=false and document results.

## Surprises & Discoveries

- None yet.

## Decision Log

- Decision: Treat new trigger logic as primary path and retire existing bracket/strategy dispatch for the MVP to avoid conflicting behaviors.
  Rationale: The goal is to rely on TopstepX Auto OCO and avoid legacy local stop/TP management.
  Date/Author: 2025-01-08 / ChatGPT

## Outcomes & Retrospective

Pending implementation.

## Context and Orientation

The webhook entry point in `tradingview_projectx_bot.py` currently coordinates market regime checks, strategy selection, and order routing with local bracket management. Supporting modules include `position_manager.py` for position context, `scheduler.py` for periodic jobs, and `signalr_listener.py` for logging/tracking. Legacy strategy helpers live in `strategies.py` and `market_regime*.py`. The new architecture should introduce dedicated modules for market state construction (`market_state.py`), decision logic (`trigger_engine.py`), and execution (`execution.py`), while simplifying the main bot orchestration and scheduler.

## Plan of Work

Introduce a `market_state.py` module that fetches recent 1m OHLCV rows from the Supabase tv_datafeed helper in `api.py`, aggregates into 5m/15m/30m bars, and computes EMA21 along with normalized slopes per timeframe. Add a `trigger_engine.py` module that consumes the market state and position context to return a HOLD/BY/SELL action plan using HOLD-first gating and slope thresholds. Create an `execution.py` module providing a minimal `send_entry` wrapper that submits market orders with a default size and no bracket logic. Refactor `tradingview_projectx_bot.py` to build market state, pull position context from `PositionManager`, run the trigger engine, and optionally place market entries when trading is enabled; remove bracket/strategy dispatch during MVP. Simplify `scheduler.py` to drop screenshot/n8n triggers and optionally call market-state evaluation. Trim SignalR usage to logging without stop enforcement or phantom sweeps. Ensure default configuration keeps `TRADING_ENABLED` false and logs clearly show market state, decisions, and order attempts.

## Concrete Steps

Perform edits from the repository root `/workspace/tradingview-bot`.

1. Add `market_state.py` implementing data retrieval, timeframe aggregation (1m to 5m/15m/30m), EMA21 calculation, normalized slope computation (linear regression over recent EMA points divided by latest price), and a `build_market_state` entry point returning a structured dict.
2. Add `trigger_engine.py` that accepts market state and position context, applies gating (get-flat windows, `PositionManager.account_metrics.can_trade`, existing positions), interprets EMA slopes to classify range vs. trend, and returns an action plan with reason codes.
3. Add `execution.py` providing `send_entry(action, acct_id, symbol, size=DEFAULT_SIZE)` that issues a single market order via ProjectX without stop/TP logic, with logging of inputs and responses.
4. Update `tradingview_projectx_bot.py` to call the new market state builder and trigger engine inside the webhook handler, guard execution with `TRADING_ENABLED`, and remove calls to legacy bracket strategy functions for the MVP.
5. Simplify `scheduler.py` to remove n8n screenshot workflows and only schedule market-state snapshots or trigger evaluation as needed. Ensure it no longer invokes bracket maintenance.
6. Adjust `signalr_listener.py` to keep logging and Supabase trade result recording but drop stop/phantom sweep enforcement tied to local bracket management.
7. Review configuration defaults to keep trading disabled; update logging to surface market state, action decisions, and order placement attempts.
8. Validate by running the Flask app with `TRADING_ENABLED=false`, hitting the webhook (or running scheduler hooks if applicable), and confirming logs show the market state and HOLD/BYE/SELL decisions without placing stops/TPs.

## Validation and Acceptance

Start the bot via `python tradingview_projectx_bot.py` and send a webhook payload to trigger evaluation. With `TRADING_ENABLED=false`, the logs should display aggregated market state, EMA slopes, decision outputs, and a note that trading is disabled. No stop or take-profit orders should be created, and only a single market entry call should be attempted when trading is enabled. Scheduler runs should no longer call screenshot workflows or bracket maintenance.

## Idempotence and Recovery

Data fetching and aggregation are read-only and safe to rerun. Market order placement is guarded by `TRADING_ENABLED` and should be used cautiously in test environments. If errors occur during webhook handling, review logs; rerunning the handler is safe because no local state is mutated beyond logging. Scheduler changes are additive and can be restarted without side effects.

## Artifacts and Notes

- Expect logs to include serialized market state, EMA slope values per timeframe, and action plan reason codes.
- Order placement logs should capture account ID, symbol, action, and size, noting when trading is disabled.

## Interfaces and Dependencies

- `market_state.build_market_state(symbol, supabase_client, bars=600)` should return a dict containing aggregated bars, EMA21 arrays, and slope metrics for 5m/15m/30m timeframes.
- `trigger_engine.decide(market_state, position_context, slope_threshold)` should return an `ActionPlan` dataclass or dict with `action` in {`BUY`, `SELL`, `HOLD`} and a `reason_code` string; it should perform gating before choosing an action.
- `execution.send_entry(action, acct_id, symbol, size, projectx_client)` should submit a single market order using existing ProjectX helpers, log the attempt, and return the response.

Changes from previous version: initial creation.
