# Reduction architecture MVP for TradingView bot

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This document must be maintained in accordance with PLANS.md at the repository root. It is self-contained so a new contributor can implement the reduction architecture MVP without prior context.

## Purpose / Big Picture

The goal is to refactor the TradingView ProjectX bot into a minimal "trade trigger" that relies solely on OHLC data from Supabase and delegates stop/target management to TopstepX Auto OCO brackets. After this change, an operator can run the bot with `TRADING_ENABLED=false` to observe regime classification and simulated actions, then enable trading to send single market entries without local stop/TP logic or screenshot-driven workflows.

## Progress

- [x] (2025-02-02 22:10Z) Drafted ExecPlan describing reduction architecture MVP and constraints.
- [x] (2025-02-02 22:50Z) Implemented Supabase-driven market state builder with EMA21 slopes and regimes.
- [x] (2025-02-02 23:05Z) Added trigger engine and execution adapter respecting risk gates and trading toggle.
- [x] (2025-02-02 23:30Z) Refactored webhook entrypoint and scheduler to reduction pipeline; legacy strategies quarantined.
- [x] (2025-02-02 23:40Z) Simplified SignalR listener to logging-only behavior with Supabase close logging.
- [ ] Final review, testing notes, and retrospective update.

## Surprises & Discoveries

- None yet.

## Decision Log

- Decision: Use a dedicated ExecPlan file (`execplan_reduction_mvp.md`) to track the reduction architecture work.
  Rationale: Keeps large refactor organized per PLANS.md and accessible to future contributors.
  Date/Author: 2025-02-02 / ChatGPT
- Decision: Disable legacy strategies and AI regime helpers while retaining code for reference.
  Rationale: Reduction MVP only needs single-shot trade triggers and avoids stop/TP logic.
  Date/Author: 2025-02-02 / ChatGPT

## Outcomes & Retrospective

- Pending.

## Context and Orientation

The bot currently combines multiple strategy modules (`strategies.py`, `market_regime*.py`) and AI-driven workflows triggered via webhook and scheduler. Orders and stop/TP management are handled locally with loops and SignalR sweeps. The refactor must:

- Add a Supabase-driven market state builder aggregating 1m data to 5m/15m/30m and computing EMA21 slopes.
- Introduce a simple trigger engine that decides BUY/SELL/HOLD based on slopes with risk gating (get-flat windows, account trade permissions, existing position guard).
- Replace webhook and scheduler orchestration with the reduction pipeline; disable legacy strategy invocations and AI/n8n image flows.
- Simplify SignalR listener to log position closes without mutating orders or stops.
- Add new configuration flags (`TRADING_ENABLED`, `DEFAULT_SIZE`, `SLOPE_LOOKBACK`, `SLOPE_THRESHOLD`, `MARKET_SYMBOL`) to control the MVP behavior.

Key files:
- `tradingview_projectx_bot.py`: Flask webhook entrypoint to be refactored to call the reduction pipeline.
- `scheduler.py`: cron-style jobs to be simplified to build market state and log decisions without sending webhooks or manipulating stops.
- `signalr_listener.py`: must retain connection and logging but remove stop/TP syncing logic.
- `config.py` and `env.example`: define environment variables.
- New modules to create: `market_state.py`, `trigger_engine.py`, `execution.py`.

## Plan of Work

1. Add configuration defaults and environment variables for trading toggle, default size, slope parameters, and market symbol in `config.py` and `env.example`.
2. Implement `market_state.py` with a `build_market_state` function that fetches recent 1m OHLCV rows for the target symbol from Supabase, aggregates to 5m/15m/30m bars, computes EMA21 and normalized slopes using linear regression over the last `SLOPE_LOOKBACK` EMA points, and returns a structured market state with regime classification and reasoning.
3. Implement `trigger_engine.py` with a `decide` function that applies gating (get-flat window, account trade permissions, existing position) and slope-based rules to output an action plan with reason codes and diagnostics.
4. Implement `execution.py` with `send_entry` that routes BUY/SELL to `api.place_market` when trading is enabled, otherwise logs a simulated order, avoiding any stop/limit placement.
5. Refactor `tradingview_projectx_bot.py` webhook handling to validate auth, honor FLAT panic flatten, compute market state, obtain position context, and call the trigger engine and execution adapter. Remove calls to legacy strategies and AI/n8n flows. Add structured logging per webhook evaluation.
6. Simplify `scheduler.py` to remove chart-image and n8n triggers, replacing the 5m job with market-state logging (optionally evaluating triggers per account without spamming webhooks). Retain get-flat job if applicable but ensure no stop placement.
7. Reduce `signalr_listener.py` to connection and logging-only behavior: keep subscriptions and closing logs to Supabase but disable stop syncing, sweeps, and phantom logic.
8. Mark legacy functions (e.g., `strategies.py` path, AI helpers in `api.py`) as unused for MVP without invoking them.
9. Validate end-to-end with `TRADING_ENABLED=false`, ensuring logs show regime, slopes, action, and reason codes for webhook calls and scheduler runs. Document manual checks in this plan.

## Concrete Steps

- Work in repository root `/workspace/tradingview-bot`.
- Update `config.py` and `env.example` with new environment variables and defaults.
- Create new modules `market_state.py`, `trigger_engine.py`, and `execution.py` implementing the functions described in the Plan of Work.
- Refactor `tradingview_projectx_bot.py`, `scheduler.py`, and `signalr_listener.py` to use the new reduction architecture and remove legacy stop/TP management.
- Run lightweight manual commands (e.g., `python tradingview_projectx_bot.py` in dev mode or targeted function calls) to ensure imports resolve and logging works with `TRADING_ENABLED=false`.
- Record observations and outcomes in this plan.

## Validation and Acceptance

The refactor is acceptable when:
- The webhook path builds market state from Supabase data, decides an action via the trigger engine, and only sends a single market order when trading is enabled and no position exists; otherwise it logs a simulated action.
- Scheduler runs no longer attempt chart-image or n8n workflows and do not spam webhooks; it can compute and log market state without placing orders.
- SignalR listener connects and logs position closures to Supabase without mutating orders or stops.
- Running the bot with `TRADING_ENABLED=false` produces log lines with regime, slopes, action, reason code, and account, and no stop/TP placement occurs.

## Idempotence and Recovery

Most changes are additive or removal of unused logic. If issues arise, re-run the bot with `TRADING_ENABLED=false` to avoid live trades. Commented or unused legacy functions should remain but not be invoked. Code can be reapplied via git checkout/reset if necessary.

## Artifacts and Notes

- Capture key log excerpts when validating webhook and scheduler flows to demonstrate regime/action logging and simulated trading paths.
- Document any Supabase query considerations (row ordering, timeframe filter) directly in `market_state.py` docstrings.

## Interfaces and Dependencies

- `market_state.build_market_state(supabase_client, symbol="MES") -> dict`: returns market snapshot including EMA21 per timeframe, slopes, regime, and reason.
- `trigger_engine.decide(market_state: dict, position_context: dict, in_get_flat: bool, trading_enabled: bool) -> dict`: returns action plan with gating reason codes and slope diagnostics.
- `execution.send_entry(action, acct_id, symbol, size, trading_enabled) -> dict`: routes BUY/SELL to `api.place_market` with side mapping BUY=0, SELL=1 when trading is enabled; otherwise returns a simulated result.

Changes should rely only on existing dependencies in `requirements.txt` (e.g., pandas/numpy if present) or pure Python; avoid introducing new heavy packages unless necessary.
