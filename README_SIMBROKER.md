# SimBroker (Local ProjectX Simulator)

This patch adds a **drop-in local broker simulator** that emulates the ProjectX Gateway REST API (enough for this repo), with:

- **Multiple sim accounts** (sim001, sim002, …) with **server-side bracket logic** (SL/TP per account).
- **Deterministic replay** via a persisted `last_processed_bar_ts` cursor per `(accountId, contractId)`.
- **No Topstep / no SignalR required** in sim mode.
- A/B testing support:
  - `SIMBROKER_IMPL=assistant` (this implementation)
  - `SIMBROKER_IMPL=codex` (Codex-generated file, same API)

---

## Quick start

### 1) Enable sim mode

In `.env`:

```env
BROKER_MODE=sim
SIMBROKER_IMPL=assistant
```

### 2) Define accounts (choose ONE)

**Option A (simple):** `ACCOUNT_` env vars

```env
ACCOUNT_SIM001=900001
ACCOUNT_SIM002=900002
```

**Option B (best for lots of accounts):** `SIM_ACCOUNTS_FILE`

```env
SIM_ACCOUNTS_FILE=./sim_accounts.json
```

A template is included: `sim_accounts.example.json`

Generate one quickly:

```bash
python tools/generate_sim_accounts.py --count 20 --out sim_accounts.json
```

**Option C:** `SIM_ACCOUNTS` list (auto ids)

```env
SIM_ACCOUNTS=sim001,sim002,sim003
SIM_ACCOUNT_ID_START=900000
```

### 3) Configure bracket rules (per account)

You can set bracket rules in **any** of the following places (higher priority wins):

1) In `SIM_ACCOUNTS_FILE` entries (`sl_usd`, `tp_usd`, `fill_policy`)
2) In `SIM_ACCOUNT_RULES_JSON` (inline JSON mapping)
3) Per-account env vars:

```env
SIM_SIM001_SL_USD=30
SIM_SIM001_TP_USD=60
SIM_SIM001_FILL_POLICY=worst   # worst|best
```

4) Global defaults:

```env
SIM_BRACKET_SL_USD=30
SIM_BRACKET_TP_USD=60
SIM_FILL_POLICY=worst
```

### 4) Configure price feed (Supabase recommended)

SimBroker pulls OHLCV from your existing `tv_datafeed` table by default if credentials exist:

```env
SUPABASE_URL=...
SUPABASE_KEY=...
```

Fallback: CSV feed

```env
SIM_BAR_FEED=csv
SIM_CSV_FEED_PATH=./tv_datafeed_5m_rows.csv
```

### 5) Run

```bash
python tradingview_projectx_bot.py
```

In sim mode the app will log:

- `BROKER_MODE=sim: skipping ProjectX auth and SignalR listener.`

---

## Persistence

SimBroker writes state to:

```env
SIMBROKER_STATE_PATH=./simbroker_state.json
```

This includes:
- accounts + balances
- orders
- positions
- trades
- bracket mappings
- last processed bar timestamp per `(accountId, contractId)`

---

## A/B testing (assistant vs codex)

1) Keep this repo patch in place (router already supports both implementations).
2) Have Codex generate `simbroker_codex.py` that exports `class SimBroker` with:
   - `handle(path: str, payload: dict) -> dict`
   - `sim_update(account_id: int, contract_id: Optional[str]=None, now_ts_iso: Optional[str]=None) -> list[dict]`

3) Switch implementation:

```env
SIMBROKER_IMPL=assistant
# or
SIMBROKER_IMPL=codex
```

---

## Tests

```bash
pytest -q
```

Included: `tests/test_simbroker_assistant.py`
