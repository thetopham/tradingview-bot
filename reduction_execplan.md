# Reduction architecture for TradingView ProjectX bot

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This document must be maintained in accordance with PLANS.md in the repository root.

## Purpose / Big Picture

The bot currently mixes AI-driven strategies, local stop/TP management, and screenshot-based workflows. The goal is to refactor toward a "reduction" trade-trigger architecture that relies solely on Supabase OHLC data, EMA slopes, and a single market order. Auto OCO brackets are handled server-side by TopstepX, so the bot should no longer place or synchronize stops locally. After implementation, a user can deploy the bot with `TRADING_ENABLED=false` to observe logged BUY/SELL/HOLD decisions derived from EMA slopes, and enable trading to send a single market order per signal without any local stop lifecycle.

## Progress

- [x] (2024-08-07 18:20Z) Drafted initial plan and repository orientation.
- [x] (2024-08-07 18:55Z) Implemented market_state.py for OHLC aggregation and EMA slopes.
- [x] (2024-08-07 18:56Z) Added trigger_engine.py gating decisions and action plans.
- [x] (2024-08-07 18:57Z) Added execution.py for market-only entries with simulation support.
- [x] (2024-08-07 19:10Z) Refactored tradingview_projectx_bot.py to reduction pipeline and logging.
- [x] (2024-08-07 19:15Z) Simplified scheduler.py to log market state without n8n triggers.
- [x] (2024-08-07 19:20Z) Trimmed signalr_listener.py to logging-only behavior.
- [x] (2024-08-07 19:21Z) Added new configuration flags to config.py/env.example.
- [x] (2024-08-07 19:30Z) Ran python -m py_compile on modified modules.

## Surprises & Discoveries

- Observation: None yet — to be filled during implementation.
  Evidence: N/A

## Decision Log

- Decision: Use a dedicated reduction_execplan.md file for this refactor to keep instructions self-contained.
  Rationale: PLANS.md requires a living, self-contained ExecPlan for complex refactors.
  Date/Author: 2024-08-07 / assistant

## Outcomes & Retrospective

To be completed after implementation to summarize behavior, remaining gaps, and lessons learned.

## Context and Orientation

Key modules today:
- tradingview_projectx_bot.py exposes Flask webhook, routes to strategies, and handles AI/n8n decisions.
- api.py wraps ProjectX endpoints for market orders, stops, Supabase access, and AI helpers.
- scheduler.py schedules cron jobs including chart/n8n triggers and stop cleanup.
- signalr_listener.py subscribes to ProjectX SignalR and currently syncs stops/positions.
- strategies.py contains local strategy dispatch including bracket logic.
- config.py/env.example hold environment defaults.

The refactor will introduce new modules:
- market_state.py: fetch 1m OHLCV from Supabase tv_datafeed, aggregate to 5m/15m/30m, compute EMA21 and normalized slopes.
- trigger_engine.py: gate decisions (get-flat, can_trade, has_position) and map slopes to BUY/SELL/HOLD regimes.
- execution.py: issue a single market order (or simulate) without local stop/TP management.

Existing helper files like market_regime.py or screenshot/n8n flows should not be used in the MVP. The SignalR listener will be limited to logging trade close events to Supabase.

## Plan of Work

1. Add new configuration keys (TRADING_ENABLED, DEFAULT_SIZE, SLOPE_LOOKBACK, SLOPE_THRESHOLD, MARKET_SYMBOL) to config.py with sensible defaults and to env.example. Ensure load_config exposes them, and any related code consumes the new values.
2. Implement market_state.py with build_market_state(supabase_client, symbol="MES"). Fetch ~600 latest 1m rows from tv_datafeed, oldest-to-newest. Aggregate to 5m/15m/30m bars, compute EMA21 per timeframe, and normalized slopes using linear regression over the last SLOPE_LOOKBACK EMA points divided by current price. Return a dict with price, EMA/slope fields, regime classification, and reasons.
3. Implement trigger_engine.py decide() that enforces get-flat, can_trade, and existing position gates before slope regimes. Produce an ActionPlan dict carrying action, reason_code, and slope/threshold context.
4. Implement execution.py send_entry() to map BUY/SELL into api.place_market calls (side 0/1). Honor trading_enabled by simulating without API calls. Do not issue stop/TP orders.
5. Refactor tradingview_projectx_bot.py handle_webhook_logic: validate secret/account, respect FLAT panic by calling flatten_contract, compute in_get_flat, build market state, query PositionManager.get_position_context_for_ai, decide via trigger_engine, and optionally execute via execution.send_entry. Remove strategy/n8n/AI decision flows and ensure logging of regime, slopes, action, reason_code, and TRADING_ENABLED per webhook.
6. Simplify scheduler.py: remove chart/n8n triggers and 45-second loops; have the cron job build_market_state and optionally log/evaluate trigger per account without spamming webhooks. Keep get-flat jobs if present but eliminate stop/TP placement.
7. Reduce signalr_listener.py to logging-only: keep connection/subscription and logging of trade closures to Supabase. Disable or remove ensure_stops_match_position, sweep_and_cleanup_positions_and_stops, and any stop management or phantom sweep behavior.
8. Quarantine unused legacy functions (strategies run_* and api AI helpers) so they are not invoked in the MVP; add comments if needed.
9. Update documentation/logging: ensure webhook evaluations log regime, slopes, action, reason_code, trading flag, and account; log simulated orders when trading is disabled.
10. Validate by running lightweight checks (e.g., python -m py_compile) and manual script invocations if applicable. Update Progress, Surprises, Decision Log, and Outcomes sections accordingly.

## Concrete Steps

- Work in repository root `/workspace/tradingview-bot`.
- Edit config.py and env.example to add the new environment variables with defaults.
- Create market_state.py, trigger_engine.py, and execution.py with functions described in Plan of Work.
- Modify tradingview_projectx_bot.py to use the new pipeline and logging, removing old strategy/AI dispatch calls for MVP.
- Simplify scheduler.py per Plan of Work and ensure it uses new market state utilities without sending orders.
- Trim signalr_listener.py to logging-only duties for position/trade updates and remove stop management.
- Run sanity checks, review logs, and commit changes with an imperative summary. Then prepare PR message per instructions.

## Validation and Acceptance

Acceptance criteria:
- With TRADING_ENABLED=false, webhook processing logs regime, slopes, action, and reason without placing orders; simulated orders are clearly logged.
- market_state.build_market_state successfully aggregates Supabase 1m data into 5m/15m/30m EMA slopes and returns a regime string.
- trigger_engine.decide honors get-flat, can_trade, and has_position gates before slope regime logic.
- execution.send_entry only issues api.place_market for BUY/SELL when trading is enabled; otherwise reports simulated.
- tradingview_projectx_bot uses the reduction pipeline and no longer references screenshot/n8n/strategy stop workflows.
- scheduler no longer triggers chart/n8n jobs or stop management; signalr_listener performs logging only.
- Code loads with `python -m py_compile` on modified modules.

## Idempotence and Recovery

Changes are additive and disabling in nature; running steps multiple times is safe. If Supabase fetch fails or no data is returned, build_market_state should fall back to an "unknown" regime without raising. Trading is controlled by TRADING_ENABLED to prevent unintended orders. If refactor disrupts startup, revert individual files via git checkout to restore prior behavior before reapplying edits.

## Artifacts and Notes

Evidence to collect: command outputs from any sanity checks (e.g., python -m py_compile), representative log lines showing simulated order handling, and git diff for new modules and refactored entrypoints.

## Interfaces and Dependencies

- market_state.build_market_state(supabase_client, symbol="MES") -> dict with keys `symbol`, `timestamp`, `price`, `ema21`, `slope`, `regime`, `reason`.
- trigger_engine.decide(market_state, position_context, in_get_flat, trading_enabled) -> dict with `action`, `reason_code`, `details`.
- execution.send_entry(action, acct_id, symbol, size, trading_enabled) -> dict summarizing whether an API call was sent.
- api.place_market(acct_id, contract_id, side, size) is the only order placement call used in MVP; stop/limit helpers remain unused.
- PositionManager.get_position_context_for_ai provides `can_trade`, `has_position`, and current contract context for gating decisions.

Note: Update this ExecPlan as work proceeds, reflecting completed steps, discoveries, and final outcomes.
