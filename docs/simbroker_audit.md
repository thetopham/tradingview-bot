# SimBroker API Audit (Baseline)

This document captures the current SimBroker/ProjectX API surface area that the bot relies on.
It reflects the *minimum* fields and endpoints referenced in code as of this audit.

## 1) api.py usage by module

Functions defined in `api.py` that are imported and used elsewhere:

- `strategies.py`
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
- `position_manager.py`
  - `search_pos`
  - `search_open`
  - `search_trades`
  - `get_contract`
  - `search_accounts`
  - `get_current_market_price`
- `dashboard.py`
  - `get_contract`
  - `get_supabase_client`
  - `reset_supabase_client`
  - `search_accounts`
- `tradingview_projectx_bot.py`
  - `flatten_contract`
  - `get_contract`
  - `ai_trade_decision`
  - `search_pos`
- `scheduler.py`
  - `flatten_contract`
  - `search_pos`

## 2) Required endpoints / paths

The bot currently calls the following HTTP endpoints:

- Orders
  - `POST /api/Order/place`
  - `POST /api/Order/searchOpen`
  - `POST /api/Order/cancel`
- Positions
  - `POST /api/Position/searchOpen`
  - `POST /api/Position/closeContract`
- Trades
  - `POST /api/Trade/search`
- Accounts
  - `POST /api/Account/search`

## 3) Minimum fields read from each payload

### Orders (`/api/Order/searchOpen` response)

Required fields currently read from each order object:

- `id` (used when canceling orders)
- `contractId`
- `type` (order type code; e.g., 1=limit, 4=stop)
- `status` (used to identify active orders)

### Positions (`/api/Position/searchOpen` response)

Required fields currently read from each position object:

- `contractId` (primary key for lookups)
- `contractSymbol` (fallback if `contractId` is missing)
- `size`
- `type` (position side code; 1=long, 2=short)
- `averagePrice` (for P&L and entry price)
- `avgPrice` (fallback for P&L summary)
- `entryPrice` (fallback for P&L summary)
- `creationTimestamp` (position duration)

### Trades (`/api/Trade/search` response)

Required fields currently read from each trade object:

- `contractId`
- `orderId`
- `price`
- `size`
- `profitAndLoss`
- `creationTimestamp`
- `voided` (used to filter trade records)
- Fee/commission fields (first non-zero wins, fallback is any key containing `fee` or `commission`)
  - `commissionAndFees`
  - `totalFees`
  - `brokerageFeesTotal`
  - `feesTotal`

### Accounts (`/api/Account/search` response)

Required fields currently read from each account object:

- `id`
- `name`
- `balance`
- `canTrade`
- `isVisible`

## 4) Assumptions and edge cases

- **Order IDs:**
  - `/api/Order/place` returns `orderId` in the placement response.
  - `/api/Order/searchOpen` returns `id` (not `orderId`) for cancel calls.
- **Position side codes:**
  - `type == 1` is treated as LONG, `type == 2` as SHORT.
- **Position IDs:**
  - `contractId` is expected, but `contractSymbol` is used as a fallback in scheduler flattening.
- **Average price keys:**
  - P&L summary code falls back in order: `avgPrice` → `averagePrice` → `entryPrice`.
- **Trade P&L on entry fills:**
  - `profitAndLoss` may be `null` for entry fills; code guards against `None`.
- **Trade filtering:**
  - `voided=True` trades are excluded from realized P&L.
- **Fees parsing:**
  - If no preferred fee field is present, any numeric key containing `fee` or `commission` is aggregated.
- **Account flags:**
  - If `canTrade` is absent, code defaults to `True`.

