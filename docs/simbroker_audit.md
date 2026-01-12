# SimBroker API Audit

## 1) api.py usage map (by caller)

**strategies.py**
- `get_contract`
- `search_pos`
- `flatten_contract`
- `place_market`
- `place_limit`
- `place_stop`
- `place_market_bracket`
- `search_open`
- `cancel`
- `search_trades`
- `log_trade_results_to_supabase`

**position_manager.py**
- `search_pos`
- `search_open`
- `search_trades`
- `search_accounts`
- `get_contract`
- `get_current_market_price` (imported lazily inside methods)

**dashboard.py**
- `get_contract`
- `get_supabase_client`
- `search_accounts`
- `reset_supabase_client` (imported lazily inside error handling)

**tradingview_projectx_bot.py**
- `flatten_contract`
- `get_contract`
- `ai_trade_decision`
- `search_pos`

**scheduler.py**
- `flatten_contract`
- `search_pos`

## 2) Required API endpoints/paths

These are the ProjectX/SimBroker endpoints the code calls today:

- **Orders**
  - `POST /api/Order/place`
  - `POST /api/Order/searchOpen`
  - `POST /api/Order/cancel`
- **Positions**
  - `POST /api/Position/searchOpen`
  - `POST /api/Position/closeContract`
- **Trades**
  - `POST /api/Trade/search`
- **Accounts**
  - `POST /api/Account/search`

## 3) Minimum response fields read by the code

### Orders (`/api/Order/searchOpen`)
Payload key: `orders` (list of order objects)

Fields accessed on each order:
- `id` (used for cancel requests)
- `contractId`
- `type` (e.g., stop/limit identification)
- `status` (used to filter active stop/limit orders)

### Positions (`/api/Position/searchOpen`)
Payload key: `positions` (list of position objects)

Fields accessed on each position:
- `contractId`
- `contractSymbol` (fallback when contractId missing)
- `type` (1=LONG, 2=SHORT)
- `size`
- `averagePrice`
- `avgPrice` (fallback)
- `entryPrice` (fallback)
- `creationTimestamp`

### Trades (`/api/Trade/search`)
Payload key: `trades` (list of trade objects)

Fields accessed on each trade:
- `orderId`
- `contractId`
- `size`
- `price`
- `side` (0=BUY, 1=SELL; used to sign quantities)
- `profitAndLoss`
- `voided`
- `creationTimestamp` (preferred)
- `timestamp` (fallback)
- Fee-related fields (if present):
  - `fees`, `commission`, `commissionAndFees`, `totalFees`, `feesTotal`, `brokerageFeesTotal`
  - Any key containing `fee` or `commission` (case-insensitive) is aggregated

### Accounts (`/api/Account/search`)
Payload key: `accounts` (list of account objects)

Fields accessed on each account:
- `id`
- `name`
- `balance`
- `canTrade`
- `isVisible`

## 4) Assumptions & guardrails noted in code

- Order placement responses return `orderId`, while order search returns `id`.
- `profitAndLoss` can be `null` on entry trades; logic guards for `None` when computing P&L.
- Trade timestamps can arrive in either `creationTimestamp` or `timestamp` and may require parsing to UTC.
- Position price fields are not consistent; code checks `averagePrice`, then `avgPrice`, then `entryPrice`.
- Some positions return only `contractSymbol`; code falls back to this when `contractId` is missing.
- Trade fees are optional; fee totals are inferred from several common fields or any key containing `fee`/`commission`.
- Trade side uses ProjectX encoding: `0=BUY`, `1=SELL`.
