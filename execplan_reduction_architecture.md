# Reduction architecture for TradingView bot

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. Maintain this document in accordance with PLANS.md.

## Purpose / Big Picture

We need to refactor the bot into a minimal "trade trigger" that relies on TopstepX server-side Auto OCO brackets instead of local stop/target handling. The new flow consumes Supabase OHLC data, computes multi-timeframe EMA slopes, decides on BUY/SELL/HOLD with strict gates, and issues a single market entry when permitted. After this change, someone can run the bot with `TRADING_ENABLED=false` to see logged action plans based on live OHLC data without any local stop placement or screenshot/AI workflows.

## Progress

- [x] (2024-05-05 12:30Z) Draft initial plan describing reduction architecture goals and scope.
- [x] (2024-05-05 13:30Z) Implemented market_state/trigger/execution modules and new config/env defaults.
- [x] (2024-05-05 13:50Z) Refactored webhook pipeline, scheduler, and SignalR listener to reduction architecture; quarantined legacy strategies/API helpers.
- [ ] Validate logging-only behavior and prepare PR.

## Surprises & Discoveries

- None yet.

## Decision Log

- Decision: Create new plan file `execplan_reduction_architecture.md` to document reduction refactor steps.
  Rationale: Major architectural change requires explicit ExecPlan per repository guidelines.
  Date/Author: 2024-05-05 / GPT-5.1-Codex-Max.
- Decision: Quarantine legacy strategy/AI helpers with RuntimeError guards while wiring new reduction pipeline.
  Rationale: Prevent accidental use of stop/target automation during MVP rollout.
  Date/Author: 2024-05-05 / GPT-5.1-Codex-Max.

## Outcomes & Retrospective

- Pending implementation.

## Context and Orientation

Current entrypoint `tradingview_projectx_bot.py` orchestrates webhook handling with strategies in `strategies.py`, data/regime helpers, and order routing via `api.py`. Scheduler tasks in `scheduler.py` and SignalR handling in `signalr_listener.py` manage stops and automation. Configuration lives in `config.py` with defaults mirrored in `env.example`. Position context is provided by `position_manager.py`, while `auth.py` controls get-flat windows and secrets.

The new architecture will add:
- `market_state.py` to build aggregated EMA/slope state from Supabase tv_datafeed rows.
- `trigger_engine.py` to convert market state plus position/account gates into an action plan.
- `execution.py` to dispatch a single market entry respecting `TRADING_ENABLED`.
These modules will be called from the webhook and scheduler, while legacy strategy and stop-management paths are quarantined.

## Plan of Work

Refactor in stages. First, create the data pipeline (market_state.py) that fetches recent 1m OHLC rows for MES from Supabase, aggregates to 5m/15m/30m bars, computes EMA21 per timeframe, and derives normalized slopes using linear regression over the last `SLOPE_LOOKBACK` EMA points. Return a structured market state with regime classification and reasoning. Next, implement `trigger_engine.py` to gate on get-flat, account trade permission, and existing positions before applying slope thresholds to choose BUY/SELL/HOLD with reason codes. Then, implement `execution.py` to send a single market order or simulate when trading is disabled. Update `config.py` and `env.example` with new environment flags for trading, size, slope parameters, and symbol.

Modify `tradingview_projectx_bot.py` webhook handling to validate secrets, handle FLAT panic via `api.flatten_contract`, compute `in_get_flat`, build market state, fetch position context, decide via trigger engine, and optionally send entry using execution module. Ensure logging records regime, slopes, action, reason, TRADING_ENABLED, and account. Remove strategy dispatch, stop placement, and n8n/LLM paths for the MVP.

Simplify `scheduler.py` to drop chart-image/n8n triggers and the 45-second delay. The 5m job should build market state and optionally evaluate triggers without spamming webhooks or placing stops. Retain get-flat maintenance only if still useful, ensuring no stop/TP management remains.

Reduce `signalr_listener.py` to logging-only: keep connection/subscription and log trade results to Supabase, but disable stop synchronization, sweeps, and any order mutations.

Finally, mark legacy functions in `strategies.py` and related API helpers as quarantined/not used in the MVP to avoid accidental invocation. Validate end-to-end with `TRADING_ENABLED=false` ensuring no orders are sent and logs show simulated actions.

## Concrete Steps

- Work from repository root `/workspace/tradingview-bot`.
- Add new modules `market_state.py`, `trigger_engine.py`, and `execution.py` with functions described above.
- Extend `config.py` and `env.example` with `TRADING_ENABLED`, `DEFAULT_SIZE`, `SLOPE_LOOKBACK`, `SLOPE_THRESHOLD`, and `MARKET_SYMBOL` defaults.
- Update `tradingview_projectx_bot.py` webhook logic to call the new pipeline and remove strategy/stop logic.
- Simplify `scheduler.py` for the new flow and remove n8n/chart triggers and stop handling.
- Strip SignalR listener down to logging-only behavior.
- Mark deprecated strategy/API functions as quarantined (docstrings/comments) and ensure they are not called in the new flow.
- Run `python -m py_compile` on touched modules or basic smoke tests if available.

## Validation and Acceptance

Acceptance: With `TRADING_ENABLED=false`, starting the Flask app (`python tradingview_projectx_bot.py`) and posting a valid webhook payload should log a single evaluation line showing regime, slopes, action (likely HOLD initially), reason code, trading_enabled flag, and account. No stop or TP orders should be placed. Scheduler and SignalR should run without attempting stop management. If `TRADING_ENABLED=true`, a BUY/SELL action should issue exactly one `place_market` call without creating stops.

## Idempotence and Recovery

Changes are additive and refactors of existing entrypoints. Running the plan multiple times is safe because new modules and config keys are deterministic. If an edit misbehaves, revert the module and re-run the steps; no migrations or destructive operations occur. Ensure `TRADING_ENABLED` remains false during testing to prevent live orders.

## Artifacts and Notes

Key evidence will include logged action plan lines during webhook tests and absence of stop/TP API calls. Code comments will mark legacy quarantine areas.

## Interfaces and Dependencies

New module interfaces:
- `market_state.build_market_state(supabase_client, symbol="MES") -> dict` returning aggregated EMA/slope regime state using Supabase tv_datafeed.
- `trigger_engine.decide(market_state, position_context, in_get_flat, trading_enabled) -> dict` returning action and reason.
- `execution.send_entry(action, acct_id, symbol, size, trading_enabled) -> dict` issuing market orders via `api.place_market` (side BUY=0, SELL=1) or simulation.

Existing dependencies: `api.py` for ProjectX market orders and contract lookup, `position_manager.py` for account metrics and position context, `auth.py` for get-flat window checks, and `logging_config.py` for consistent logging.
