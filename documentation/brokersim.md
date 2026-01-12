# BrokerSim (SimBroker) Usage

BrokerSim (the `SimBroker` class in `simbroker.py`) emulates the ProjectX API surface so the bot can place simulated orders, manage positions, and record trades without touching live accounts. It stores state in a JSON file and pulls price data from Supabase (or a custom in-memory feed in tests).

## 1) When BrokerSim is used

BrokerSim is invoked automatically whenever one of these conditions is true:

- `BROKER_MODE=sim` (global simulation mode), or
- The account name starts with `sim` (e.g., `ACCOUNT_SIM1=20001`).

This behavior is handled in `api.py` by `_use_sim_broker`, which routes API calls to `SimBroker` instead of ProjectX when sim mode is active or the account name is prefixed with `sim`.

## 2) Required environment configuration

Add these to your `.env` (see `env.example` for defaults):

```bash
# Required to force sim mode (optional if you only use sim-prefixed accounts)
BROKER_MODE=sim

# Sim broker state file (persisted between runs)
SIM_STATE_PATH=./simbroker_state.json

# Define sim accounts (JSON string or path)
SIM_ACCOUNTS_JSON={"sim001":{"id":20001,"balance":50000,"sl_usd":30,"tp_usd":60}}
# or
SIM_ACCOUNTS_PATH=/path/to/sim_accounts.json

# Price feed (Supabase table tv_datafeed)
SUPABASE_URL=...
SUPABASE_KEY=...
```

Notes:

- `SIM_ACCOUNTS_JSON`/`SIM_ACCOUNTS_PATH` are only required if you want to define sim accounts dynamically. They are merged into the standard `ACCOUNT_<NAME>=<ID>` map at startup.
- `SIM_STATE_PATH` is the JSON file where BrokerSim stores accounts, orders, positions, and trades. Delete it to reset the sim broker state.

## 3) Optional tuning for bracket fills and pricing

BrokerSim uses these settings to size bracket orders and compute PnL:

```bash
SIM_DEFAULT_BALANCE=50000
SIM_DEFAULT_SL_USD=30
SIM_DEFAULT_TP_USD=60
SIM_RISK_BASIS=per_position  # or per_contract
SIM_FILL_POLICY=worst        # or best
SIM_STARTING_BALANCE=50000
SIM_BRACKET_SL_USD=30
SIM_BRACKET_TP_USD=60
SIM_PRICE_TIMEFRAME=1m       # falls back to 5m
SIM_DEFAULT_TICK_SIZE=0.25
SIM_DEFAULT_TICK_VALUE=1.25
```

Per-account overrides can be set by environment prefix:

```bash
SIM_ACCOUNT_SIM001_SL_USD=40
SIM_ACCOUNT_SIM001_TP_USD=80
```

## 4) Running the bot in sim mode

1. Configure your `.env` with the values above.
2. Start the bot:

```bash
python tradingview_projectx_bot.py
```

When sim mode is active, calls in `api.py` (for orders, positions, and trades) route to BrokerSim instead of ProjectX. The state file is updated as orders are placed and bars are processed.

## 5) How fills and updates work

- **Market orders** fill immediately using the latest close from Supabase (`tv_datafeed`).
- **Limit/stop orders** fill when the simulated bar data reaches the limit/stop price.
- **Bracket orders** are auto-created on market entries and are filled based on the bar’s high/low and the `SIM_FILL_POLICY` (best/worst).

Simulation is advanced via `/api/sim/update` or `api.sim_update`, which the code calls automatically before reading positions/orders for sim accounts. You can also invoke it manually if you are working directly with `SimBroker`.

## 6) Common workflows

### Create a simulated account via JSON

```bash
SIM_ACCOUNTS_JSON={"sim001":{"id":20001,"balance":50000,"sl_usd":30,"tp_usd":60}}
ACCOUNT_SIM001=20001
```

### Reset sim state

Delete the state file:

```bash
rm -f simbroker_state.json
```

### Keep using live accounts alongside sim accounts

- Leave `BROKER_MODE=live`.
- Add a sim-prefixed account in `.env` (e.g., `ACCOUNT_SIM001=20001`).
- Calls to that account route to BrokerSim; other accounts still use ProjectX.

## 7) Troubleshooting

- **`NO_MARKET_DATA` errors**: ensure Supabase credentials are set and `tv_datafeed` contains bars for the requested symbol/timeframe.
- **State not persisting**: verify `SIM_STATE_PATH` points to a writeable location.
- **Orders never fill**: confirm the `SIM_PRICE_TIMEFRAME` matches available data (1m or 5m) and that `sim_update` is being called.

## 8) API surface emulated by BrokerSim

BrokerSim handles the ProjectX paths used by this repo:

- `POST /api/Account/search`
- `POST /api/Auth/loginKey`
- `POST /api/Auth/validate`
- `POST /api/Contract/available`
- `POST /api/Contract/search`
- `POST /api/Contract/searchById`
- `POST /api/History/retrieveBars`
- `POST /api/Order/place`
- `POST /api/Order/search`
- `POST /api/Order/searchOpen`
- `POST /api/Order/cancel`
- `POST /api/Order/modify`
- `POST /api/Position/searchOpen`
- `POST /api/Position/closeContract`
- `POST /api/Position/partialCloseContract`
- `POST /api/Trade/search`
- `POST /api/sim/update`

