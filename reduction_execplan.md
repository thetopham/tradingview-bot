# Implement reduction trigger architecture

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This plan must remain in sync with `PLANS.md` at the repository root.

## Purpose / Big Picture

Refactor the bot into a simple trade trigger that reads OHLCV data, derives a trend/range regime, and issues a single market entry when conditions align. Stop relying on screenshot-driven workflows and local stop/target logic because TopstepX now applies Auto OCO brackets server-side. Success is demonstrated by running the webhook path with `TRADING_ENABLED=false`, observing logs that show market-state snapshots, action plans, and skipped order placement due to the trading flag.

## Progress

- [x] (2025-12-24 02:26Z) Drafted initial ExecPlan capturing architecture shift, scope, and validation approach.
- [x] (2025-12-24 02:30Z) Build market_state module to fetch/aggregate OHLCV and compute EMA slopes.
- [x] (2025-12-24 02:30Z) Add trigger engine to derive BUY/SELL/HOLD with hold-first gates.
- [x] (2025-12-24 02:30Z) Create execution helper for bracket-free market entries and integrate logging.
- [x] (2025-12-24 02:30Z) Rewire tradingview_projectx_bot.py to use new trigger flow and disable legacy strategies.
- [x] (2025-12-24 02:30Z) Simplify scheduler to drop n8n chart triggers and optionally snapshot/evaluate on interval.
- [x] (2025-12-24 02:30Z) Clean up SignalR usage (logging only) and remove local bracket enforcement.
- [ ] Run sanity checks with TRADING_ENABLED=false and document observed logs.

## Surprises & Discoveries

- None yet. Populate as implementation reveals unexpected behavior or datafeed quirks.

## Decision Log

- Decision: Keep TRADING_ENABLED default as false while wiring the new trigger path so dry runs are safe by default.
  Rationale: Prevent accidental live orders during refactor and allow validation via logs only.
  Date/Author: 2025-12-24 / Assistant

## Outcomes & Retrospective

Pending implementation. Will summarize final behavior, remaining gaps, and lessons learned once work concludes.

## Context and Orientation

The current bot (tradingview_projectx_bot.py) handles TradingView webhooks, dispatches strategies in `strategies.py`, manages positions via `position_manager.py`, and schedules chart-fetching plus webhook triggers in `scheduler.py`. SignalR logging lives in `signalr_listener.py`. Market regime helpers use screenshot-based n8n workflows and local bracket management functions (`run_bracket`, `run_brackmod`, `run_pivot`).

New architecture requirements:
- Use Supabase `tv_datafeed` table containing 1m OHLCV rows as the sole data source. Aggregate to 5m/15m/30m and compute EMA21 with normalized slope per timeframe.
- Determine regime per timeframe and select BUY/SELL/HOLD based on slope thresholds, with HOLD-first gates for flat windows and existing positions.
- Execute only one market order via ProjectX API, no stop/TP management; rely on server-side Auto OCO template. PositionManager risk gates (`account_metrics.can_trade`) still apply.
- Scheduler should avoid n8n chart workflows; optional periodic market-state snapshot/evaluation is acceptable.
- SignalR listener remains for logging; remove local stop syncing and phantom sweeps tied to bracket handling.

Key files to touch:
- New modules under repo root: `market_state.py`, `trigger_engine.py`, `execution.py`.
- Update `tradingview_projectx_bot.py`, `scheduler.py`, and `signalr_listener.py` (for logging scope changes).
- Coordinate with `position_manager.py` for account gating and context helpers.

## Plan of Work

Describe edits in sequence so a newcomer can follow:
1. Create `market_state.py` with functions to fetch the latest ~600 1m bars from Supabase (via existing Supabase client helpers), aggregate to 5m/15m/30m using pandas or manual resampling, compute EMA21 on closes, and derive normalized linear-regression slopes over a small window. Return a `MarketState` dict capturing raw/aggregated bars, EMA values, slopes, and per-timeframe regime classification.
2. Create `trigger_engine.py` that exposes `decide(market_state, position_context)` returning an ActionPlan (`action` in {BUY, SELL, HOLD}, `reason_code`). Implement hold-first checks: flat window (`auth.in_get_flat`), `position_context` presence, `PositionManager.account_metrics.can_trade` false, mixed/weak slopes. Use slope sign for BUY/SELL when above threshold across timeframes; otherwise HOLD.
3. Add `execution.py` with `send_entry(action, acct_id, symbol, size=DEFAULT_SIZE)` wrapping ProjectX `place_market` call (via existing `api` helpers). Log intent and skip when `TRADING_ENABLED` is false. Do not create local stop/target orders. Keep extension point for optional LLM reasoning later.
4. Refactor `tradingview_projectx_bot.py` webhook handling to replace strategy dispatch with new flow: build market state, fetch position context (`PositionManager.get_position_context_for_ai()` or equivalent), call `trigger_engine.decide`, honor TRADING_ENABLED gate, and call `send_entry` for BUY/SELL only. Remove calls to `run_bracket`, `run_brackmod`, `run_pivot`, and any local SL/TP enforcement. Ensure logs print market state summary and action plan.
5. Simplify `scheduler.py` to stop triggering n8n chart-image workflows. Optionally schedule market-state snapshot plus decision evaluation every 5 minutes when not in flat window. Remove bracket maintenance hooks. Keep or adjust rollover/risk logging as needed.
6. Update `signalr_listener.py` (and any helper) to remove stop-sync/phantom sweep hooks, limiting SignalR usage to logging and trade close notifications. Ensure Supabase logging of trade results persists.
7. Verify configuration defaults keep trading disabled. Run the bot locally (`python tradingview_projectx_bot.py` with TRADING_ENABLED=false) and observe logs showing market-state computation, HOLD/BUT/SELL decision, and skipped order placement because trading is disabled. Capture commands and expected output in Concrete Steps and Validation sections.

## Concrete Steps

Working directory: `/workspace/tradingview-bot`.
- Read `config.py`, `api.py`, `position_manager.py`, `scheduler.py`, and `tradingview_projectx_bot.py` to map existing helpers for Supabase, ProjectX orders, and risk gates.
- Implement modules and refactors per Plan of Work using the repository’s logging conventions.
- Run `python -m compileall .` or `python tradingview_projectx_bot.py --help` to surface syntax issues (no test suite exists). With TRADING_ENABLED=false, start the bot and trigger webhook manually via curl to observe logs.
- Update this ExecPlan’s Progress, Decision Log, and Outcomes as milestones complete.

Expected command examples:
- `python tradingview_projectx_bot.py` (start server for manual webhook tests; expect logs showing market state generation and HOLD decision when trading disabled).
- `curl -X POST http://localhost:5000/webhook -H "Content-Type: application/json" -d '{"secret":"<WEBHOOK_SECRET>","account":"<account_name>","signal":"hold"}'` to exercise the path without placing orders.

## Validation and Acceptance

Acceptance is behavioral:
- With TRADING_ENABLED=false, invoking the webhook logs market-state aggregation results (per timeframe EMA/slope/regime) and an ActionPlan decision, and explicitly notes that trading is disabled so no order is sent.
- With TRADING_ENABLED=true (optional dry-run using a safe account), webhook reaches `execution.send_entry` and logs a single market order intent without attempting to manage stops/targets locally.
- Scheduler no longer calls n8n chart-image endpoints; periodic evaluations (if enabled) use the new market-state builder and trigger engine.
- SignalR remains active for logging but no longer runs ensure_stops_match_position or phantom order sweeps.

## Idempotence and Recovery

Changes are additive and config-driven. Re-running the scheduler or webhook while TRADING_ENABLED=false is safe and produces repeated logs without external side effects. If Supabase fetch fails, guard with logging and early HOLD decisions. Rolling back is a matter of restoring previous versions of modified files; no migrations are required.

## Artifacts and Notes

Capture key log excerpts demonstrating market-state calculation, decision rationale, and skipped execution due to TRADING_ENABLED=false. Include any manual curl payloads used for validation. No external screenshots or n8n artifacts are needed.

## Interfaces and Dependencies

- Supabase client from `api.get_supabase_client()` used to query `tv_datafeed` 1m OHLCV rows.
- Position context via `position_manager.PositionManager` helpers (existing `get_position_context_for_ai` and account metrics). Respect `account_metrics.can_trade` and get-flat window (`auth.in_get_flat`).
- ProjectX order placement via existing `api.place_market` or equivalent. `execution.send_entry` should wrap this with parameters `(acct_id, symbol, side, size)` and logging.
- Logging uses module-level `logging.getLogger(__name__)` with contextual messages for market state, decisions, and execution results.
