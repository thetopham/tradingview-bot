# Broker Compatibility Layer Design

Date: 2026-05-07
Repo: `/home/thetopham/tradingview-bot`
Status: design plus Milestone 1 dry-run proof

## Goal

Replace the dead TopstepX / SignalR dependency while preserving the current logging, Supabase, n8n, dashboard, and analysis pipeline contracts.

The compatibility layer should let the bot run in these modes:

- `projectx`: current TopstepX/ProjectX behavior, while it still exists.
- `paper`: synthetic fills using live/current price sources, no real broker.
- `sim`: deterministic replay/backtest fills from local or Supabase datafeed rows.
- future `crypto`: normalized exchange adapter with legacy-compatible logging output.

The first implementation rule is: do not break the old logging contract. PaperBroker and SimBroker should produce normalized events internally, then expose ProjectX-shaped compatibility dictionaries where old code expects them.

## Non-goals for the first pass

Do not change yet:

- Supabase table names or column names.
- n8n workflows.
- dashboard queries.
- report scripts.
- `trade_results.raw_trades` shape.
- AI overseer payload shape.
- TradingView `/webhook` payload shape.

## 1. Current event flow

### A. TradingView/n8n/scanner decision path

```text
TradingView / scanner / scheduler / n8n
  -> Flask POST /webhook in tradingview_projectx_bot.py
  -> handle_webhook_logic(data)
  -> account alias resolved via ACCOUNTS
  -> symbol resolved via get_contract(sym)
  -> optional AI/n8n overseer via ai_trade_decision()
  -> strategy dispatch: run_simple(...)
  -> api.place_market(...)
  -> ProjectX /api/Order/place
  -> signalr_listener.track_trade(...) stores entry metadata
```

Important current contracts:

- `/webhook` accepts `strategy`, `account`, `signal`, `symbol`, `size`, `alert`, `ai_decision_id`, `secret`.
- Strategy helpers expect ProjectX-style order responses with `orderId` and sometimes `fillPrice`.
- `track_trade()` stores metadata keyed by `accountId|contractId`.

### B. Current TopstepX close/logging path

```text
TopstepX SignalR user hub
  -> GatewayUserOrder
  -> on_order_update(args)
  -> enrich trade_meta with entry_time/order_id/entry_price

TopstepX SignalR user hub
  -> GatewayUserPosition
  -> on_position_update(args)
  -> when size == 0: pop trade_meta
  -> api.log_trade_results_to_supabase(acct_id, cid, entry_time, ai_decision_id, meta)
  -> ProjectX /api/Trade/search
  -> derive round-trip fills, prices, PnL, fees
  -> Supabase trade_results update/insert
```

Important current contracts:

- SignalR position close event is the trigger for `trade_results` logging.
- `log_trade_results_to_supabase()` expects ProjectX trade dictionaries from `/api/Trade/search`.
- `trade_results.raw_trades` stores those ProjectX-like raw trade dictionaries.

### C. Current Supabase/reporting path

```text
trade_results
  -> ai_trade_feed view/table
  -> dashboard.py
  -> reports/run_daily_reports.py
  -> reports/upload_daily_report.py
  -> n8n analysis/report assumptions
```

Tables/views to preserve initially:

- `ai_trading_log`
- `trade_results`
- `ai_trade_feed`
- `charts`
- `tv_datafeed`
- `tv_datafeed_5m`
- `tv_datafeed_15m`
- `tv_datafeed_30m`
- `latest_chart_analysis`

## 2. Proposed normalized event model

The compatibility layer should use normalized event records internally, then render them into legacy ProjectX-compatible dictionaries for old call sites.

### Common event envelope

```json
{
  "event_id": "evt_...",
  "event_type": "order_filled",
  "broker": "sim",
  "broker_account_id": "paper",
  "legacy_account_id": 999001,
  "symbol": "MES",
  "instrument_id": "CON.F.US.MES.SIM",
  "occurred_at": "2026-05-07T06:00:00Z",
  "source": "sim_broker",
  "raw": {}
}
```

### Normalized event types

#### `account_updated`

```json
{
  "event_type": "account_updated",
  "account": {
    "broker": "sim",
    "broker_account_id": "paper",
    "legacy_account_id": 999001,
    "name": "paper",
    "balance": 50000.0,
    "can_trade": true,
    "raw": {}
  }
}
```

#### `order_submitted`

```json
{
  "event_type": "order_submitted",
  "order": {
    "broker": "sim",
    "broker_order_id": "SIM-ENTRY-0001",
    "legacy_order_id": "SIM-ENTRY-0001",
    "account_id": 999001,
    "symbol": "MES",
    "instrument_id": "CON.F.US.MES.SIM",
    "side": "BUY",
    "order_type": "MARKET",
    "quantity": 1,
    "status": "submitted",
    "limit_price": null,
    "stop_price": null,
    "created_at": "2026-05-07T06:00:00Z",
    "raw": {}
  }
}
```

#### `order_filled`

```json
{
  "event_type": "order_filled",
  "order": {
    "broker_order_id": "SIM-ENTRY-0001",
    "legacy_order_id": "SIM-ENTRY-0001",
    "status": "filled"
  },
  "fill": {
    "broker_fill_id": "SIM-TRADE-ENTRY-0001",
    "legacy_trade_id": "SIM-TRADE-ENTRY-0001",
    "account_id": 999001,
    "symbol": "MES",
    "instrument_id": "CON.F.US.MES.SIM",
    "side": "BUY",
    "quantity": 1,
    "price": 5000.0,
    "realized_pnl": null,
    "fees": 1.2,
    "filled_at": "2026-05-07T06:00:00Z",
    "raw": {}
  }
}
```

#### `position_updated`

```json
{
  "event_type": "position_updated",
  "position": {
    "account_id": 999001,
    "symbol": "MES",
    "instrument_id": "CON.F.US.MES.SIM",
    "side": "LONG",
    "quantity": 1,
    "average_price": 5000.0,
    "unrealized_pnl": 0.0,
    "opened_at": "2026-05-07T06:00:00Z",
    "raw": {}
  }
}
```

#### `position_closed`

```json
{
  "event_type": "position_closed",
  "position": {
    "account_id": 999001,
    "symbol": "MES",
    "instrument_id": "CON.F.US.MES.SIM",
    "side": "FLAT",
    "quantity": 0,
    "average_price": null,
    "realized_pnl": 42.5,
    "fees": 2.4,
    "closed_at": "2026-05-07T06:01:00Z",
    "raw": {}
  }
}
```

## Compatibility views

Every normalized event should support legacy ProjectX rendering while old code still consumes ProjectX-like dicts.

### Legacy order compatibility

```json
{
  "id": "SIM-ENTRY-0001",
  "orderId": "SIM-ENTRY-0001",
  "accountId": 999001,
  "contractId": "CON.F.US.MES.SIM",
  "type": 2,
  "side": 0,
  "size": 1,
  "status": 2,
  "averageFillPrice": 5000.0,
  "fillPrice": 5000.0,
  "creationTimestamp": "2026-05-07T06:00:00Z"
}
```

### Legacy trade/fill compatibility

```json
{
  "id": "SIM-TRADE-ENTRY-0001",
  "accountId": 999001,
  "contractId": "CON.F.US.MES.SIM",
  "orderId": "SIM-ENTRY-0001",
  "side": 0,
  "size": 1,
  "price": 5000.0,
  "profitAndLoss": null,
  "fees": 1.2,
  "creationTimestamp": "2026-05-07T06:00:00Z",
  "voided": false
}
```

### Legacy position compatibility

```json
{
  "accountId": 999001,
  "contractId": "CON.F.US.MES.SIM",
  "type": 1,
  "size": 1,
  "averagePrice": 5000.0,
  "creationTimestamp": "2026-05-07T06:00:00Z"
}
```

## 3. Topstep event -> normalized event mapping

### `GatewayUserAccount`

| Topstep field | Normalized field | Notes |
|---|---|---|
| `id` / `accountId` | `account.legacy_account_id` | Keep int ID for legacy routing. |
| `name` | `account.name` | Also used to resolve account alias where available. |
| `balance` | `account.balance` | Preserve as numeric. |
| `canTrade` | `account.can_trade` | Boolean. |
| full event | `raw` | Store unmodified for debugging. |

Normalized event:

- `event_type=account_updated`
- `broker=projectx`
- `source=topstepx_signalr`

### `GatewayUserOrder`

| Topstep field | Normalized field | Compatibility field |
|---|---|---|
| `id` / `orderId` | `order.broker_order_id` | `orderId`, `id` |
| `accountId` | `order.account_id` | `accountId` |
| `contractId` | `order.instrument_id` | `contractId` |
| `side` 0/1 | `order.side` BUY/SELL | `side` 0/1 |
| `type` 1/2/4 | `order.order_type` LIMIT/MARKET/STOP | `type` 1/2/4 |
| `size` | `order.quantity` | `size` |
| `status` | `order.status` | `status` |
| `limitPrice` | `order.limit_price` | `limitPrice` |
| `stopPrice` | `order.stop_price` | `stopPrice` |
| `creationTimestamp` | `order.created_at` | `creationTimestamp` |
| `averageFillPrice` / `fillPrice` | fill price | `averageFillPrice`, `fillPrice` |

Topstep status mapping should be confirmed against API docs, but current code treats `status == 2` as filled in `signalr_listener.py`.

### `GatewayUserPosition`

| Topstep field | Normalized field | Compatibility field |
|---|---|---|
| `accountId` | `position.account_id` | `accountId` |
| `contractId` | `position.instrument_id` | `contractId` |
| `type` 1/2 | `position.side` LONG/SHORT | `type` 1/2 |
| `size` | `position.quantity` | `size` |
| `averagePrice` / `avgPrice` | `position.average_price` | `averagePrice` |
| `creationTimestamp` | `position.opened_at` | `creationTimestamp` |
| `size == 0` | `position_closed` | close event trigger |

### `GatewayUserTrade`

| Topstep field | Normalized field | Compatibility field |
|---|---|---|
| `id` | `fill.broker_fill_id` | `id` |
| `accountId` | `fill.account_id` | `accountId` |
| `contractId` | `fill.instrument_id` | `contractId` |
| `orderId` | `fill.broker_order_id` | `orderId` |
| `side` 0/1 | `fill.side` BUY/SELL | `side` 0/1 |
| `size` | `fill.quantity` | `size` |
| `price` | `fill.price` | `price` |
| `profitAndLoss` | `fill.realized_pnl` | `profitAndLoss` |
| `fees` / commission fields | `fill.fees` | `fees` |
| `creationTimestamp` | `fill.filled_at` | `creationTimestamp` |
| `voided` | `fill.voided` | `voided` |

## 4. SimBroker event -> normalized event mapping

SimBroker should generate normalized events first, then ProjectX-compatible dicts.

### SimBroker internal fill

```json
{
  "sim_fill_id": "SIM-TRADE-ENTRY-0001",
  "sim_order_id": "SIM-ENTRY-0001",
  "account": "paper",
  "legacy_account_id": 999001,
  "symbol": "MES",
  "instrument_id": "CON.F.US.MES.SIM",
  "side": "BUY",
  "quantity": 1,
  "price": 5000.0,
  "realized_pnl": null,
  "fees": 1.2,
  "filled_at": "2026-05-07T06:00:00Z"
}
```

Mapping:

| SimBroker field | Normalized field | ProjectX-compatible field |
|---|---|---|
| `sim_fill_id` | `fill.broker_fill_id` | `id` |
| `sim_order_id` | `fill.broker_order_id` | `orderId` |
| `legacy_account_id` | `fill.account_id` | `accountId` |
| `instrument_id` | `fill.instrument_id` | `contractId` |
| `side` BUY/SELL | `fill.side` | `side` 0/1 |
| `quantity` | `fill.quantity` | `size` |
| `price` | `fill.price` | `price` |
| `realized_pnl` | `fill.realized_pnl` | `profitAndLoss` |
| `fees` | `fill.fees` | `fees` |
| `filled_at` | `fill.filled_at` | `creationTimestamp` |

### SimBroker position close

SimBroker should emit a `position_closed` normalized event once synthetic position quantity returns to zero. The compatibility dispatcher can then call the existing logger:

```python
log_trade_results_to_supabase(
    acct_id=legacy_account_id,
    cid=instrument_id,
    entry_time=entry_time,
    ai_decision_id=ai_decision_id,
    meta=legacy_trade_meta,
)
```

For Milestone 1, the dry-run script bypasses the full event dispatcher and calls this final logger directly with monkey-patched dependencies.

## 5. Minimal fake fill event example

This is the smallest useful ProjectX-compatible round trip for the existing logger:

```json
[
  {
    "id": "SIM-TRADE-ENTRY-0001",
    "accountId": 999001,
    "contractId": "CON.F.US.MES.SIM",
    "orderId": "SIM-ENTRY-0001",
    "side": 0,
    "size": 1,
    "price": 5000.0,
    "profitAndLoss": null,
    "fees": 1.2,
    "creationTimestamp": "2026-05-07T06:00:00Z",
    "voided": false,
    "raw_source": "sim_broker_dry_run"
  },
  {
    "id": "SIM-TRADE-EXIT-0001",
    "accountId": 999001,
    "contractId": "CON.F.US.MES.SIM",
    "orderId": "SIM-EXIT-0001",
    "side": 1,
    "size": 1,
    "price": 5008.5,
    "profitAndLoss": 42.5,
    "fees": 1.2,
    "creationTimestamp": "2026-05-07T06:01:00Z",
    "voided": false,
    "raw_source": "sim_broker_dry_run"
  }
]
```

Why this works with the current logger:

- `contractId` matches the `cid` argument.
- `orderId` on the entry fill matches `meta.order_id`.
- `side` uses ProjectX `0=BUY`, `1=SELL`.
- `size` returns to flat across the two fills.
- The exit fill has `profitAndLoss`, so retry logic stops.
- Fees are present and become `fees_total`.
- `creationTimestamp` gives entry/exit timestamps.

## 6. Files that need to change

These should change in later implementation milestones, not all at once.

### New files to add

- `broker_compat/` or `brokers/`
  - `models.py`: normalized event/account/order/position/fill types.
  - `compat_projectx.py`: render normalized records into ProjectX-shaped dicts.
  - `adapter.py`: broker adapter protocol/interface.
  - `projectx_adapter.py`: wraps current `api.post()` behavior.
  - `paper_broker.py`: paper fills from current price source.
  - `sim_broker.py`: deterministic synthetic fills/replay.
  - `dispatcher.py`: routes normalized events into legacy logging functions.

### Existing files to change later

- `config.py`
  - Add `BROKER_MODE` and explicit paper/sim defaults.
  - Avoid requiring ProjectX credentials for paper/sim mode.

- `api.py`
  - Keep public helper names.
  - Move ProjectX-specific HTTP calls behind `ProjectXAdapter`.
  - Route helper calls through selected adapter.
  - Keep `log_trade_results_to_supabase()` stable initially.

- `tradingview_projectx_bot.py`
  - Select broker mode on startup.
  - Do not call `authenticate()` or launch SignalR in paper/sim mode.
  - Eventually rename only after behavior is stable.

- `signalr_listener.py`
  - Extract event handler logic from SignalR transport.
  - Make `track_trade()` and close-event dispatch available without importing `signalrcore`.

- `strategies.py`
  - Keep calls to `place_market()`, `search_pos()`, etc. initially.
  - Later accept a broker service/dependency explicitly.

- `position_manager.py`
  - Read normalized positions eventually.
  - Until then, consume compatibility dicts.

- `scheduler.py`
  - Skip live flatten jobs or route them through PaperBroker/SimBroker in non-live modes.

- `dashboard.py`
  - No first-pass change; later read broker-neutral views only after schema migration.

### Maintenance/test files

- `scripts/dry_run_simulated_fill.py`
  - Milestone 1 proof that a fake fill can reach the legacy logging payload path.

- `tests/test_dry_run_simulated_fill.py`
  - Regression test that the dry-run path makes no live broker calls and preserves payload shape.

## 7. Files that should not change yet

Do not change these in the first compatibility-layer pass unless a later milestone explicitly scopes it:

- `n8n/**/*.json`
  - They encode current ai/log/chart assumptions. Keep stable until adapter output is proven.

- Supabase schema / SQL migrations
  - Do not rename or drop current tables/views.

- `reports/run_daily_reports.py`
  - Depends on `ai_trading_log.urls` and daily report output conventions.

- `reports/upload_daily_report.py`
  - Storage upload flow can stay as-is.

- `dashboard.py` templates/static files
  - Let dashboard continue reading `ai_trade_feed` and live compatibility snapshots.

- `upload_botlog.py`
  - Historical log-storage behavior can remain until process naming/log paths are deliberately changed.

- Existing runtime state files
  - `trade_state.json`, `market_state*.json`, and daily report folders should not be deleted or rewritten by migration scripts.

## 8. Step-by-step implementation plan

### Milestone 1: dry-run fake fill through legacy logger

Status: implemented.

Implemented file:

- `scripts/dry_run_simulated_fill.py`

Test file:

- `tests/test_dry_run_simulated_fill.py`

What it does:

1. Creates two ProjectX-shaped synthetic trade dictionaries: entry and exit.
2. Monkey-patches `api.post()` so `/api/Trade/search` returns the fake trades.
3. Monkey-patches `api.get_supabase_client()` so idempotency lookups are local/no-op.
4. Monkey-patches `api.session.post()` so the `trade_results` insert is captured locally instead of sent to Supabase.
5. Calls `api.log_trade_results_to_supabase()`.
6. Prints and optionally writes the captured would-be `trade_results` payload.

Safety boundary:

- No live broker calls.
- No real order placement.
- No SignalR import/connection.
- No real Supabase write.

Run:

```bash
python scripts/dry_run_simulated_fill.py --output /tmp/tradingview-bot-dry-fill.json --log-level INFO
```

Expected output includes:

- `live_broker_calls: false`
- `signalr_required: false`
- `broker_calls: ["/api/Trade/search"]`
- `supabase_insert_url: http://supabase-dry-run.invalid/rest/v1/trade_results`
- payload preview with `total_pnl=42.5`, `fees_total=2.4`, `net_pnl=40.1`

Test:

```bash
python -m unittest tests.test_dry_run_simulated_fill -v
```

### Milestone 2: extract compatibility models

Add pure data/model layer:

- `brokers/models.py`
- `brokers/compat_projectx.py`

Acceptance criteria:

- Normalized `BrokerFill` can render to the exact fake ProjectX trade shape from Milestone 1.
- Unit tests cover side enum mapping, position type mapping, order type mapping, timestamps, fees, and PnL fields.
- No production call sites changed yet.

### Milestone 3: add event dispatcher without changing broker execution

Add:

- `brokers/dispatcher.py`

Dispatcher responsibilities:

- Accept normalized events.
- Maintain or delegate `trade_meta` lifecycle.
- On `position_closed`, call `log_trade_results_to_supabase()` with compatibility trade search injected by adapter.

Acceptance criteria:

- Milestone 1 dry run can be rewritten to use dispatcher while still producing identical payload.
- No SignalR dependency required.

### Milestone 4: ProjectX adapter wrapper

Add:

- `brokers/projectx_adapter.py`

Move ProjectX HTTP endpoint calls behind adapter methods while keeping `api.py` helper names.

Acceptance criteria:

- `api.place_market()`, `search_pos()`, `search_trades()`, etc. still work in `BROKER_MODE=projectx`.
- No dashboard/n8n/report changes.

### Milestone 5: PaperBroker

Add:

- `brokers/paper_broker.py`

Responsibilities:

- Generate synthetic order IDs.
- Fill using `get_current_market_price()` or explicit test price.
- Maintain in-memory/local state for orders, positions, and fills.
- Return ProjectX-shaped compatibility dicts through existing helper names.
- Emit close events through dispatcher.

Acceptance criteria:

- `BROKER_MODE=paper` does not call ProjectX auth or SignalR.
- One paper BUY then FLAT produces a `trade_results` payload with legacy shape.
- Clear startup log says paper mode is active and live trading is disabled.

### Milestone 6: SimBroker replay

Add:

- `brokers/sim_broker.py`

Responsibilities:

- Replay `tv_datafeed_*` rows.
- Deterministically fill at configured bar price: open/close/VWAP.
- Emit normalized events and compatibility trades.

Acceptance criteria:

- Same strategy decision can be replayed deterministically.
- No live broker/Supabase write unless explicitly configured.

### Milestone 7: schema/n8n migration planning

Only after PaperBroker/SimBroker compatibility is stable:

- Add optional `broker`, `instrument_id`, `normalized_event`, and `compat_version` fields.
- Build broker-neutral views.
- Update n8n prompts from MES-specific text to instrument-config text.
- Keep backward compatibility views until reports/dashboard are migrated.

## Open design questions

1. Should synthetic paper fills write to real Supabase by default, or require an explicit `PAPER_WRITE_SUPABASE=true` flag?
   - Recommendation: require explicit flag.

2. Should `raw_trades` remain ProjectX-shaped forever?
   - Recommendation: preserve in v1, add `raw_trades_version` or `broker_raw_events` later.

3. Should `signalr_listener.track_trade()` be moved out before PaperBroker?
   - Recommendation: yes. It currently imports `signalrcore`, so extracting trade metadata lifecycle will reduce coupling.

4. Should the app file be renamed from `tradingview_projectx_bot.py`?
   - Recommendation: not yet. Rename after behavior is stable to avoid service/deployment breakage.
