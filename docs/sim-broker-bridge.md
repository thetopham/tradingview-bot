# Plugging the v2 simulator into the legacy bot

The legacy bot's original broker boundary was `api.py` plus the SignalR listener. `BROKER_MODE=sim` uses the persistent `tradingview-bot-v2` SQLite ledger for account, position, bracket order, and trade-fill views. It accepts decisions through the existing `/webhook` route. In this mode, startup does not authenticate to ProjectX, launch SignalR, or start the legacy broker scheduler. Direct ProjectX API calls are rejected before authentication.

The legacy service on the Pi remains disabled. The bridge is intended to run as a separate local instance until the feed and result dispatch are validated.

## Configuration

Install `requirements-sim.txt` (or install the local v2 checkout into the same environment). Point the bridge to an **already initialized** v2 database:

```text
BROKER_MODE=sim
SIM_BROKER_DB=/home/thetopham/tradingview-bot-v2/data/sim_broker.sqlite
TV_PORT=5001
WEBHOOK_SECRET=<local secret>
DASHBOARD_PASSWORD=<local password>
SIM_MAX_BAR_LAG_SECONDS=600
```

Simulation mode reads account names from the v2 ledger at startup. It assigns stable compatibility account IDs from SQLite row IDs, starting at 900001. Add accounts with the v2 `init --portfolio` command, then restart this legacy bridge to refresh its account map. Profile names must be lowercase for legacy webhook routing. New accounts can use `N8N_OVERSEER_URL_<ACCOUNT>`; the historical alpha through practice URL variables continue to work. `PROJECTX_*` settings are unused in sim mode.

`SIM_MAX_BAR_LAG_SECONDS` rejects stale bars in forward operation. Set it to `0` only for an isolated replay or test database.

## Webhook contract

Submit a **closed** MES bar with a timezone-aware **bar-open** timestamp. A supplied `decision` skips the legacy n8n call; without one, the configured overseer receives the simulated position and account context. If no overseer URL exists, the webhook's own signal is used. Each call advances exactly one account and bar.

```json
{
  "secret": "<local secret>",
  "account": "epsilon",
  "bar": {
    "timestamp": "2026-09-22T14:00:00Z",
    "open": 6000.0,
    "high": 6001.0,
    "low": 5999.0,
    "close": 6000.5,
    "volume": 1200
  },
  "decision": {
    "signal": "BUY",
    "size": 1,
    "source": "prodex",
    "prompt_version": "simple-30m-epsilon-1-13-2026-optimize"
  }
}
```

The response contains the updated ledger snapshot. A BUY/SELL/FLAT decision acts no earlier than the next contiguous bar open. If the same bar is retried without an explicit decision, the bridge returns the saved snapshot before calling n8n again. Duplicate decisions are checked by the v2 broker. `GET /healthz` remains the local health check. The bridge binds to `127.0.0.1` when run directly; this keeps its webhook off the public interface during validation.

## Shared datafeed route

`POST /sim/feed?source_table=tv_datafeed_30m` accepts one completed row from the existing n8n `datafeed_30m` Supabase node. The same route supports `tv_datafeed_5m` and `tv_datafeed_15m`. Pass `X-Webhook-Secret`; the JSON body can be the inserted Supabase row (`ts`, `o`, `h`, `l`, `c`, `v`, `symbol`, `timeframe`) or `{ "row": <that row> }`. The v2 feed normalizer requires MES and the matching timeframe and infers bar open only when the row's receipt timestamp is within two minutes after the bar close. Late or malformed rows are rejected. A TradingView-provided bar timestamp takes precedence when available.

The route fans the bar out to **every** configured account with the matching timeframe. Each account can call its own n8n overseer URL. For isolated replay, an optional `decisions` object may contain account-keyed decisions; production feed rows have none. The response lists account snapshots and any per-account errors. Retrying a bar is safe. This route is implemented but the active n8n datafeed workflows have not yet been connected to it.

`GET /sim/events?after_id=0&limit=100` returns normalized ledger events and a `next_cursor` for the next poll. Pass `X-Webhook-Secret` in the request header. `GET /sim/results?after_id=0&limit=100` returns durable, ProjectX-shaped `trade_results` payloads built from closed v2 trades. The outbox is backfilled from the ledger when the adapter starts and uses the v2 trade ID plus account and generation as its unique key. This replaces SignalR's position-close trigger without coupling simulated execution to delivery.

To check how many results are awaiting Supabase, set `SIM_BROKER_DB` and run:

```bash
.venv/bin/python scripts/publish_sim_results.py --dry-run
```

Once a valid `SUPABASE_URL` and `SUPABASE_KEY` are configured, run the same command without `--dry-run`. It checks `trace_id` in `trade_results` before inserting and marks each outbox row only after a successful response. A failed delivery remains pending for the next run. Run this from a single scheduled worker. The command does not affect fills or account state.

## Current boundary and next checks

- The v2 ledger is authoritative for fills, fees, positions, trailing loss, and pass/fail events. The bridge renders ProjectX-shaped read views for the old dashboard/position manager without making a broker call.
- Closed simulated trades are queued locally in `sim_result_outbox` and exposed through `/sim/results`. Delivery to Supabase is implemented as a separate command, but it has **not been enabled on the Pi** because the available legacy key is invalid against local Supabase. The `ai_trade_feed` view has not yet been checked against newly published simulated rows.
- The live n8n workflow currently pulls its own feed. It has not been changed to include a canonical closed bar in the webhook response. Until it does, submit only test envelopes to the local bridge. A missing bar is rejected rather than guessed from wall-clock time or a stale quote.
- The active `datafeed_5m`, `datafeed_15m`, and `datafeed_30m` workflows produce a Supabase row with a receipt timestamp. Their post-insert output can feed `/sim/feed` after the bridge is reachable from the n8n container and a header credential has been configured. Do not put the secret in exported workflow JSON.
- The Pi's legacy `.env` points at an obsolete Supabase host. Local Supabase at `192.168.0.35:8000` is reachable but requires a valid key. Do not reuse the old key or start the old scheduler for this bridge.
