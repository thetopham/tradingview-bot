# Trading Bot Coupling Audit: Broker, Market, Symbol, and Schema

Date: 2026-05-07
Repo: `/home/thetopham/tradingview-bot`
Scope: audit/report-only pass over the current TradingView bot, ProjectX/TopstepX broker integration, SignalR listener, Supabase logging/reporting paths, scanner/report scripts, and exported n8n workflows.

## Executive summary

The bot is tightly coupled to the original TopstepX / ProjectX / SignalR / MES futures stack in four layers:

1. Broker HTTP/auth client: `auth.py` and `api.py` directly encode ProjectX login, order, account, position, and trade endpoints.
2. Realtime broker events: `signalr_listener.py` directly depends on TopstepX SignalR hub events and subscription names.
3. Instrument/market assumptions: `MES`, `CON.F.US.MES.*`, MES point value, futures position types, and 5m/15m/30m loops appear across Python and n8n workflows.
4. Logging/reporting contracts: Supabase tables, views, storage buckets, and n8n workflows assume the existing `ai_trading_log`, `trade_results`, `ai_trade_feed`, `charts`, and `tv_datafeed*` shapes.

The safest migration is not to rewrite n8n or Supabase first. Instead, freeze the current logging contracts, insert a broker compatibility layer underneath the existing helper functions, and have PaperBroker/SimBroker emit ProjectX-shaped compatibility dictionaries until the analysis pipeline is migrated.

No secrets are quoted in this report. Any live credential values found in env/config are intentionally omitted.

## Scope and constraints

Requested search terms:

- Topstep
- TopstepX
- ProjectX
- SignalR
- MES
- CON.F.US.MES
- AccountId / accountId
- ContractId / contractId
- OrderId / orderId
- Position
- Bracket
- FillPrice / fillPrice
- OrderStatus / orderStatus

Excluded generated/vendor/cache directories:

- `.git`
- `node_modules`
- `.venv`
- `venv`
- `__pycache__`
- `dist`
- `build`
- `.pytest_cache`
- `.mypy_cache`
- `.cache`

Constraints followed:

- No production code changes for the audit itself.
- No deletions.
- No n8n workflow changes.
- Existing Supabase/n8n/reporting assumptions are treated as compatibility contracts.

## High-level search results

Approximate targeted counts across non-vendor repo files:

| Term family | Approx. matches | Approx. files | Notes |
|---|---:|---:|---|
| Topstep | 155 | 35 | Mostly docs/n8n prompt text plus `auth.py` logging. |
| TopstepX | 50 | 4 | Mainly TopstepX docs and SignalR URL. |
| ProjectX | 43 | 12 | Core broker naming/config/API helper coupling. |
| SignalR | 76 | 8 | `signalr_listener.py`, docs, requirement. |
| MES | 258 | 52 | Python defaults, n8n prompts, chart/datafeed filters. |
| CON.F.US.MES | 70 | 35 | Contract IDs in config, runtime state, n8n workflows. |
| accountId / acct_id / ACCOUNTS | 267 | 19 | Broker API payload shape and app routing. |
| contractId / cid / OVERRIDE_CONTRACT_ID | 218 | 20 | Broker API payload shape and symbol resolution. |
| orderId / order_id | 75 | 4 | Order placement/fill correlation. |
| Position / position | 3005 | 181 | Very broad; includes n8n node coordinates, so only semantic matches matter. |
| Bracket / bracket | 728 | 33 | Bracket strategies and n8n workflow names/prompts. |
| fillPrice / price | 356 | 30 | Includes market data price fields; important in order/fill/PnL paths. |
| orderStatus / status | 78 | 18 | ProjectX-like order status and HTTP status use. |

## Broker/API coupling findings

### `config.py`

Important lines:

- `config.py:11` loads `PROJECTX_BASE_URL` into `PX_BASE`.
- `config.py:12` loads `PROJECTX_USERNAME`.
- `config.py:13` loads `PROJECTX_API_KEY`.
- `config.py:37-40` builds `ACCOUNTS` from `ACCOUNT_*` env vars.
- `config.py:48` defaults `OVERRIDE_CONTRACT_ID` to `CON.F.US.MES.H26`.
- `config.py:49-54` defaults futures risk/point settings: `STOP_LOSS_POINTS`, `TP_POINTS`, `TICKS_PER_POINT`.

Impact:

- Broker credentials and account IDs are assumed at import time.
- A future PaperBroker/SimBroker needs safe defaults that do not require live ProjectX credentials.
- Contract override is instrument-specific and expiry-specific.

### `auth.py`

Important lines:

- `auth.py:12-15` imports broker config from `load_config()`.
- `auth.py:56-73` authenticates to Topstep/ProjectX through `PX_BASE + /api/Auth/loginKey`.
- `auth.py:81-84` `ensure_token()` can trigger live authentication when `api.post()` is used.

Impact:

- Any caller of `api.post()` can accidentally authenticate to a live/dead broker unless the broker call is intercepted.
- Broker mode selection should occur before `ensure_token()`.

### `api.py`

`api.py` is the central coupling point. It is both the broker client and shared application API.

Important lines:

- `api.py:17-24` loads `ACCOUNTS`, `OVERRIDE_CONTRACT_ID`, `PX_BASE`, Supabase credentials, and hardcodes `MES = "MES"`.
- `api.py:71-94` defines `post(path, payload)`, which always uses `PX_BASE`, bearer auth, and the global requests session.
- `api.py:97-137` places ProjectX orders through `/api/Order/place`.
- `api.py:118-131` encodes ProjectX server-side bracket fields `stopLossBracket` and `takeProfitBracket`.
- `api.py:139-170` wraps `/api/Order/searchOpen`, `/api/Order/cancel`, `/api/Position/searchOpen`, `/api/Account/search`, `/api/Position/closeContract`, `/api/Trade/search`.
- `api.py:433-481` computes lightweight PnL using `averagePrice`, `type`, `size`, current Supabase price, and MES multiplier.
- `api.py:570-1185` persists closed-trade results to Supabase via `log_trade_results_to_supabase()`.

ProjectX endpoint coupling:

- `/api/Order/place`
- `/api/Order/searchOpen`
- `/api/Order/cancel`
- `/api/Position/searchOpen`
- `/api/Position/closeContract`
- `/api/Account/search`
- `/api/Trade/search`

ProjectX field/enum coupling:

- `accountId`
- `contractId`
- `orderId`
- `type`
- `side`
- `size`
- `limitPrice`
- `stopPrice`
- `stopLossBracket`
- `takeProfitBracket`
- `creationTimestamp`
- `averagePrice` / `avgPrice`
- `profitAndLoss`
- `voided`
- order side `0=BUY`, `1=SELL`
- position type `1=LONG`, `2=SHORT`
- order type `1=limit`, `2=market`, `4=stop`

Impact:

- Do not replace call sites first. Keep existing helper names and route internally through a broker adapter.
- PaperBroker/SimBroker should emit ProjectX-shaped compatibility dicts until `log_trade_results_to_supabase()`, dashboard, and reports are migrated.

### `tradingview_projectx_bot.py`

Important lines:

- `tradingview_projectx_bot.py:5-6` names the app the ProjectX Trading Bot.
- `tradingview_projectx_bot.py:12-19` imports broker helpers and SignalR listener directly.
- `tradingview_projectx_bot.py:57-65` exposes `/webhook` and starts async processing.
- `tradingview_projectx_bot.py:72-88` resolves webhook payload into account, signal, symbol, size, and `cid = get_contract(sym)`.
- `tradingview_projectx_bot.py:90-113` handles manual `FLAT` by calling `flatten_contract()`.
- `tradingview_projectx_bot.py:120-157` routes to n8n/AI overseer and includes live positions.
- `tradingview_projectx_bot.py:176-203` handles AI `FLAT` by annotating exit intent and flattening.
- `tradingview_projectx_bot.py:240-243` dispatches to `run_simple()`.
- `tradingview_projectx_bot.py:250-257` authenticates, launches SignalR, and starts scheduler on process startup.

Impact:

- `/webhook` is the main external decision entry point and should be preserved.
- Startup should eventually select broker mode. In paper/sim mode it should not authenticate or start SignalR.

### `strategies.py`

Important lines:

- `strategies.py:7-12` imports ProjectX-shaped helper functions and `track_trade()`.
- `strategies.py:38-95` `run_simple()` resolves contract, checks/flat positions, places a market order, derives fill price, and calls `track_trade()`.
- `strategies.py:64-65` calls `place_market()` and reads `orderId` / `fillPrice`.
- `strategies.py:77-95` writes entry metadata into the SignalR trade tracker.
- `strategies.py:97-292` contains older/commented bracket/pivot logic with bracket, stop, limit, order ID, and trade search assumptions.

Impact:

- Strategies are tightly coupled to ProjectX helper return shapes.
- Migration should keep these helper names stable at first.

### `position_manager.py`

Important lines:

- `position_manager.py:14` imports `search_pos`, `search_open`, `search_trades`, `get_contract`, `search_accounts` from `api.py`.
- `position_manager.py:203`, `289-290`, `342`, `349` assume `contractId`, `averagePrice`, `type`, `profitAndLoss`.
- `position_manager.py:246` and `361` call `get_current_market_price(symbol="MES")`.
- `position_manager.py:364` hardcodes MES multiplier `5`.

Impact:

- Position snapshots and dashboard metrics need broker-neutral normalized positions, but should receive compatibility dicts initially.

## SignalR / realtime event coupling

### `signalr_listener.py`

Important lines:

- `signalr_listener.py:11` imports `signalrcore.hub_connection_builder.HubConnectionBuilder`.
- `signalr_listener.py:16` hardcodes TopstepX user hub URL: `wss://rtc.topstepx.com/hubs/user?access_token={}`.
- `signalr_listener.py:23-26` stores `orders_state`, `positions_state`, `trade_meta`, and `recent_closures`.
- `signalr_listener.py:211-263` `track_trade()` records entry metadata keyed by account/contract.
- `signalr_listener.py:438-441` registers handlers for `GatewayUserAccount`, `GatewayUserOrder`, `GatewayUserPosition`, `GatewayUserTrade`.
- `signalr_listener.py:476-482` sends `SubscribeAccounts`, `SubscribeOrders`, `SubscribePositions`, `SubscribeTrades`.
- `signalr_listener.py:547-581` `on_order_update()` updates entry metadata from filled order events.
- `signalr_listener.py:584-707` `on_position_update()` detects position size `0`, pops metadata, and calls `log_trade_results_to_supabase()`.
- `signalr_listener.py:738-744` can create a minimal log entry if metadata is missing.

Impact:

- SignalR is not just transport; it is part of the trade lifecycle tracker.
- A replacement PaperBroker/SimBroker must either call the same final logger or emit equivalent order/position/trade events into a compatibility dispatcher.
- The first milestone can bypass SignalR by calling `log_trade_results_to_supabase()` directly with fake broker trade search data.

## Market, symbol, timeframe, and account coupling

### MES / contract coupling

Key examples:

- `api.py:24` hardcodes `MES = "MES"`.
- `api.py:252` defaults `get_current_market_price(symbol="MES")`.
- `api.py:463-464` uses MES current-price lookup and multiplier.
- `config.py:48` defaults `OVERRIDE_CONTRACT_ID` to `CON.F.US.MES.H26`.
- `dashboard.py:457` calls `get_contract("MES")` for open-position snapshot.
- `position_manager.py:246` and `361` call `get_current_market_price(symbol="MES")`.
- `position_manager.py:364` hardcodes MES multiplier `5`.
- `scheduler.py:73-78` sends chart prefetch payload with `symbol: "MES"`.
- `scheduler.py:105` defaults overseer symbol to `CON.F.US.MES.H26`.
- `market_regime.py:49-51` defaults tables by timeframe: `tv_datafeed_5m`, `tv_datafeed_15m`, `tv_datafeed_30m`.
- `scanner.py:319-320` defaults scanner symbol/table to `MES` and `tv_datafeed_5m`.
- `market_scanner_breakout_retest.py:604`, `710` default scanner symbol to `MES`.
- `n8n/chart_fetch/* chart fetch.json` contains hardcoded MES filter conditions.
- `n8n/overseers/**/*.json` contains MES prompt text and `CON.F.US.MES.*` contract IDs.

Impact:

- Migration to crypto cannot treat symbol as only a futures contract ID.
- Add an instrument registry, but preserve `symbol` and `contractId` fields in legacy payloads during transition.

### Timeframe/account routing coupling

Observed workflow/account shape:

- alpha, beta, gamma, practice: primarily 5m loops.
- delta: 15m loop.
- epsilon: 30m loop.
- `tradingview_projectx_bot.py:38-45` maps account aliases to `N8N_OVERSEER_URL_TEST1` through `N8N_OVERSEER_URL_TEST6`.
- `scheduler.py:168-245` has jobs for chart prefetch and optional account-specific overseer triggers.

Impact:

- Timeframe/account mapping should be config-driven later, but n8n should not be rewritten in the first migration pass.

## External event entry points

### 1. TradingView / scanner / n8n webhook into Flask

File: `tradingview_projectx_bot.py`

Route:

- `POST /webhook`

Flow:

1. `tv_webhook()` receives JSON and checks `WEBHOOK_SECRET`.
2. `handle_webhook_logic()` runs in a background thread.
3. Payload fields are extracted: `strategy`, `account`, `signal`, `symbol`, `size`, `alert`, `ai_decision_id`.
4. Account alias is resolved through `ACCOUNTS`.
5. Symbol is resolved through `get_contract()`.
6. Manual or AI `FLAT` calls `flatten_contract()`.
7. Non-flat signals call `ai_trade_decision()` if an n8n account endpoint is configured.
8. Final BUY/SELL dispatch calls `run_simple()`.
9. `run_simple()` places a broker order and calls `track_trade()`.

### 2. TopstepX SignalR broker events

File: `signalr_listener.py`

Event handlers:

- `GatewayUserAccount` -> `on_account_update()`
- `GatewayUserOrder` -> `on_order_update()`
- `GatewayUserPosition` -> `on_position_update()`
- `GatewayUserTrade` -> `on_trade_update()`

Flow:

1. Listener authenticates and connects to TopstepX user hub.
2. It subscribes to accounts, orders, positions, and trades.
3. `on_order_update()` enriches entry metadata when an order fills.
4. `on_position_update()` tracks open/closed position state.
5. When position size becomes `0`, it calls `log_trade_results_to_supabase()`.

### 3. Scheduler events

File: `scheduler.py`

Jobs:

- chart prefetch posts to n8n chart endpoints.
- overseer triggers post to local `/webhook`.
- force-flat job polls positions and calls `flatten_contract()`.
- daily report job launches report scripts.

### 4. Scanner events

Files:

- `scanner.py`
- `market_scanner_breakout_retest.py`

Flow:

1. Scanner reads `tv_datafeed_*` rows.
2. It detects setup/resumption events.
3. It posts to local `/webhook` or directly to n8n.

### 5. n8n workflow events

Workflow groups:

- `n8n/datafeeds/*.json`
- `n8n/chart_fetch/*.json`
- `n8n/overseers/**/*.json`

Flow:

- Datafeed workflows write TradingView bar data into Supabase.
- Chart fetch workflows update chart URLs/snapshots.
- Overseer workflows read datafeed/log tables, call AI, write `ai_trading_log`, and emit bot decisions.

## Supabase and storage read/write paths

### Python write paths

| File | Table/bucket | Operation | Notes |
|---|---|---|---|
| `api.py:570-1185` | `trade_results` | select/update/insert | Main closed-trade logger. Uses ProjectX `/api/Trade/search` and raw trade dicts. |
| `scripts/backfill_trade_prices.py:124-173` | `trade_results` | select/update | Maintenance backfill for entry/exit price fields. |
| `upload_botlog.py:46,55,72` | storage bucket `botlogs` | upload/list/remove | Uploads `/tmp/tradingview_projectx_bot.log*`. |
| `reports/upload_daily_report.py:27` | storage bucket `daily-reports` by default | upload | Uploads daily report bundle and summary. |

### Python read paths

| File | Table/view | Use |
|---|---|---|
| `api.py:284,348,391` | `tv_datafeed` | latest price lookup. |
| `api.py:309` | `latest_chart_analysis` | current-price fallback. |
| `api.py:990` | `ai_trading_log` | recover `ai_decision_id` near entry time. |
| `api.py:1091,1103` | `trade_results` | idempotency lookup before insert. |
| `dashboard.py:224,264` | `ai_trade_feed` | dashboard/feed view. |
| `market_regime.py:49-51,151` | `tv_datafeed_5m`, `tv_datafeed_15m`, `tv_datafeed_30m` | regime calculation. |
| `scanner.py:287,319-345` | `tv_datafeed_5m` by default | scanner input. |
| `market_scanner_breakout_retest.py:201,709-710` | `tv_datafeed_30m` by default | scanner input. |
| `reports/run_daily_reports.py:205` | `ai_trading_log` | chart URL/report collection. |

### n8n write/read paths

| Workflow group | Table | Operation |
|---|---|---|
| `n8n/datafeeds/datafeed.json` | `tv_datafeed` | create rows. |
| `n8n/datafeeds/datafeed_5m.json` | `tv_datafeed_5m` | create rows. |
| `n8n/datafeeds/datafeed_15m.json` | `tv_datafeed_15m` | create rows. |
| `n8n/datafeeds/datafeed_30m.json` | `tv_datafeed_30m` | create rows. |
| `n8n/chart_fetch/5m chart fetch.json` | `charts` | update. |
| `n8n/chart_fetch/15m chart fetch.json` | `charts` | update. |
| `n8n/chart_fetch/30m chart fetch.json` | `charts` | update. |
| `n8n/overseers/**/*.json` | `ai_trading_log` | create/update/read. |
| `n8n/overseers/**/*.json` | `tv_datafeed_5m`, `tv_datafeed_15m`, `tv_datafeed_30m` | read rows by workflow/account timeframe. |
| `n8n/overseers/**/*.json` | `charts` | get/update. |

## Legacy logging contract to preserve

`api.log_trade_results_to_supabase()` builds the current `trade_results` payload. The compatibility layer should preserve this shape first:

```json
{
  "strategy": "simple",
  "signal": "BUY",
  "symbol": "CON.F.US.MES.H26",
  "account": "practice",
  "size": 1,
  "ai_decision_id": 123,
  "entry_time": "...",
  "exit_time": "...",
  "duration_sec": 120,
  "alert": "...",
  "total_pnl": 42.5,
  "fees_total": 2.4,
  "net_pnl": 40.1,
  "entry_price": 5000.0,
  "exit_price": 5008.5,
  "entry_price_source": "trade_fills_vwap",
  "exit_price_source": "trade_fills_vwap",
  "raw_trades": [],
  "order_id": "[\"...\"]",
  "comment": "...",
  "trade_ids": [],
  "trace_id": "...",
  "session_id": "...",
  "prompt_version": "...",
  "exit_ai_decision_id": null,
  "exit_reason": "...",
  "exit_signal": "FLAT",
  "exit_trigger": "...",
  "exit_requested_at": "..."
}
```

`raw_trades` should continue to contain ProjectX-shaped dictionaries until the reporting stack is ready for a versioned normalized raw-event schema.

## Migration risks

1. Calling `api.post()` currently implies ProjectX auth/token flow.
2. SignalR currently drives close-event detection and logging; removing it without a replacement event dispatcher will break `trade_results` writes.
3. `trade_results.raw_trades` stores broker-native ProjectX trade dictionaries.
4. PnL math and position state assume futures side/type enums and MES multiplier.
5. n8n workflows embed MES prompt text, table names, account/timeframe routing, and chart/Supabase assumptions.
6. Dashboard/report scripts read compatibility views/tables and should not be migrated first.

## Minimal migration path

### Phase 0: freeze current contracts

Do not change these yet:

- `ai_trading_log`
- `trade_results`
- `ai_trade_feed`
- `charts`
- `tv_datafeed`
- `tv_datafeed_5m`
- `tv_datafeed_15m`
- `tv_datafeed_30m`
- `latest_chart_analysis`
- n8n workflow payload shapes
- dashboard/report field expectations

### Phase 1: add Broker Compatibility Layer behind existing helper names

Keep callers stable:

- `place_market()`
- `place_limit()`
- `place_stop()`
- `place_market_bracket()`
- `search_open()`
- `search_pos()`
- `search_accounts()`
- `search_trades()`
- `close_pos()`
- `flatten_contract()`
- `get_contract()`

Route those functions internally to a selected adapter:

- `BROKER_MODE=projectx`
- `BROKER_MODE=paper`
- `BROKER_MODE=sim`
- future `BROKER_MODE=crypto`

### Phase 2: define normalized broker events and records

Canonical records:

- account
- order
- position
- fill/trade
- position closed

Each normalized event should keep `raw` and emit a ProjectX compatibility view.

### Phase 3: implement PaperBroker

PaperBroker should:

- never authenticate to ProjectX.
- never connect to SignalR.
- use current price sources for synthetic fills.
- generate synthetic order IDs and fill IDs.
- maintain local open position/order/fill state.
- emit ProjectX-shaped compatibility trades.
- call the existing `log_trade_results_to_supabase()` path.

### Phase 4: implement SimBroker

SimBroker should:

- replay `tv_datafeed_*` bars deterministically.
- fill at deterministic bar prices.
- produce normalized events and ProjectX-shaped compatibility trades.
- support backtests without changing n8n first.

### Phase 5: add CryptoBroker later

CryptoBroker should:

- handle 24/7 sessions.
- support decimal size/quantity.
- support exchange-specific fees and partial fills.
- map symbols to legacy `symbol`/`contractId` fields until n8n/reporting are migrated.

## Validation checklist

Before replacing the live broker path:

- [ ] A dry-run simulated fill can create the same `trade_results` payload shape without ProjectX auth.
- [ ] The dry-run path makes zero live broker calls.
- [ ] The dry-run path does not import or require SignalR.
- [ ] `raw_trades` contains ProjectX-shaped compatibility dicts.
- [ ] Dashboard/report queries still work against existing tables/views.
- [ ] Existing n8n workflows can remain unchanged.
- [ ] PaperBroker mode cannot place live orders by construction.
- [ ] Broker mode is explicit and logs its selected mode on startup.

## Milestone 1 result

A dry-run proof script now exists at:

- `scripts/dry_run_simulated_fill.py`

It generates one synthetic entry fill and one synthetic exit fill, monkey-patches the broker/Supabase dependencies used by `api.log_trade_results_to_supabase()`, and captures the would-be `trade_results` insert payload without live broker calls, without SignalR, and without a real Supabase write.

Run it with:

```bash
python scripts/dry_run_simulated_fill.py --output /tmp/tradingview-bot-dry-fill.json --log-level INFO
```

Run its regression test with:

```bash
python -m unittest tests.test_dry_run_simulated_fill -v
```
