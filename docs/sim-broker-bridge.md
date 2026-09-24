# Plugging the v2 simulator into the legacy bot

The legacy bot's original broker boundary was `api.py` plus the SignalR listener. `BROKER_MODE=sim` uses the persistent `tradingview-bot-v2` SQLite ledger for account, position, bracket order, and trade-fill views. It accepts decisions through the existing `/webhook` route. In this mode, startup does not authenticate to ProjectX, launch SignalR, or start the legacy broker scheduler. Direct ProjectX API calls are rejected before authentication.

The legacy service on the Pi remains disabled. The bridge is intended to run as a separate local instance until the feed and result dispatch are validated.

## Configuration

Install `requirements-sim.txt` (or install the local v2 checkout into the same environment). Point the bridge to an **already initialized** v2 database:

On the Pi, the bridge virtual environment uses an editable install of `/home/thetopham/tradingview-bot-v2`. After pulling v2 changes, verify that `tvbot_v2.simulate.ledger.__file__` points into that checkout. A stale installed wheel can leave the bridge running old ledger code even after both repositories are updated. The systemd override uses two Gunicorn workers and a 120-second timeout so one-minute bars can continue while a 30-minute ProDex request is in flight.

```text
BROKER_MODE=sim
SIM_BROKER_DB=/home/thetopham/tradingview-bot-v2/data/sim_broker.sqlite
TV_PORT=5001
WEBHOOK_SECRET=<local secret>
DASHBOARD_PASSWORD=<local password>
SIM_MAX_BAR_LAG_SECONDS=600
SIM_DECISION_SOURCE=feed
```

Simulation mode reads account names from the v2 ledger at startup. It assigns stable compatibility account IDs from SQLite row IDs, starting at 900001. Add accounts with the v2 `init --portfolio` command, then restart this legacy bridge to refresh its account map. Profile names must be lowercase for legacy webhook routing. New accounts can use `N8N_OVERSEER_URL_<ACCOUNT>`; the historical alpha through practice URL variables continue to work. `PROJECTX_*` settings are unused in sim mode.

`SIM_MAX_BAR_LAG_SECONDS` rejects stale bars in forward operation. Set it to `0` only for an isolated replay or test database.

For the Pi's separate decision scheduler, set `SIM_DECISION_SOURCE=scheduler` and install `deploy/tradingview-bot-sim-decisions.service` and `.timer`. The existing 30-minute n8n datafeed still forwards its saved candle, but `/sim/feed` caches it without calling the AI. Each timer run checks for a fresh cached candle and invokes the configured overseer once per account and candle. The resulting decision enters the local broker and is filled from the next eligible one-minute bar. This setting avoids running both the feed and scheduler as decision triggers. Accounts without configured overseer URLs are skipped. The old live-broker scheduler and its chart jobs remain disabled.

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

The original scheduler payload, which has **no `bar` object**, now also works at `/webhook` in sim mode. The bridge calls the configured overseer with the simulated account/position context, accepts its BUY/SELL/HOLD/FLAT result, and submits a broker order only after that response arrives. An explicit `decision` can be supplied by a caller that already ran an overseer. The order waits for the next one-minute open; the 1m feed never calls the model. `client_order_id` or the overseer's `ai_decision_id` makes a retry idempotent. A missing overseer and missing explicit decision are rejected.

The bridge also renders `api.place_market`, account, position, open-order, trade, cancel, and flatten calls against the local ledger in sim mode. `GET /sim/broker-events` provides cursor-based `GatewayUserAccount`, `GatewayUserOrder`, `GatewayUserPosition`, and `GatewayUserTrade` messages from the same durable ledger events. It replaces the data the Topstep SignalR listener supplied; it is an authenticated polling endpoint, not a SignalR WebSocket server. Closed trades still enter `/sim/results` for the existing reporting shape.

## Shared datafeed route

`POST /sim/feed?source_table=tv_datafeed_30m` accepts one completed row from the existing n8n `datafeed_30m` Supabase node. The same route supports `tv_datafeed_5m` and `tv_datafeed_15m`. Pass `X-Webhook-Secret`; the JSON body can be the inserted Supabase row (`ts`, `o`, `h`, `l`, `c`, `v`, `symbol`, `timeframe`) or `{ "row": <that row> }`. The v2 feed normalizer requires MES and the matching timeframe and infers bar open only when the row's receipt timestamp is within two minutes after the bar close. Late or malformed rows are rejected. A TradingView-provided bar timestamp takes precedence when available.

`POST /sim/feed?source_table=tv_datafeed` accepts the one-minute MES rows for accounts whose `execution_timeframe` is `1m`. It advances fills, brackets, marked equity, and risk without calling an overseer. It rejects a `decisions` object. The row's `ts` is a post-close receipt timestamp; the normalizer assigns the preceding one-minute bar only when the receipt is at most 50 seconds after its minute boundary. A TradingView bar-open timestamp in the alert is preferable because a delayed receipt can otherwise be assigned to the wrong minute. The simulation's 1-minute OHLC cannot reveal the order of stop and target touches within the minute, so it charges the stop in that case.

For a profile with 30-minute decisions and 1-minute execution, the 30-minute route records the ProDex decision separately and queues it for the first eligible one-minute bar **whose open follows the response time**. This prevents a delayed model response from receiving an earlier candle's open price. The original `epsilon` account remains a 30-minute execution baseline; `epsilon_1m` is a separate profile that can use the same ProDex workflow and a different execution feed.

The route fans the bar out to **every** configured account with the matching timeframe. Each account can call its own n8n overseer URL. For isolated replay, an optional `decisions` object may contain account-keyed decisions; production feed rows have none. The response lists account snapshots and any per-account errors. Retrying a bar is safe.

On the Pi, the published `datafeed_30m` workflow forwards its inserted Supabase row to this route with a dedicated n8n Header Auth credential. The bridge binds to Pi loopback and the private n8n Docker gateway at port 5001. The 5m and 15m workflows remain unchanged. Epsilon calls the separate published `MES 30m ProDex numeric simulator` workflow at `/webhook/simple30m-sim-prodex`. That workflow uses the local feed and continuity lookup and excludes the expired chart-image service. Its direct test returned a valid decision. The original chart workflow remains available separately.

`GET /sim/events?after_id=0&limit=100` returns normalized ledger events and a `next_cursor` for the next poll. Pass `X-Webhook-Secret` in the request header. `GET /sim/results?after_id=0&limit=100` returns durable, ProjectX-shaped `trade_results` payloads built from closed v2 trades. The outbox is backfilled from the ledger when the adapter starts and uses the v2 trade ID plus account and generation as its unique key. This replaces SignalR's position-close trigger without coupling simulated execution to delivery.

To check how many results are awaiting Supabase, set `SIM_BROKER_DB` and run:

```bash
.venv/bin/python scripts/publish_sim_results.py --dry-run
```

Once a valid `SUPABASE_URL` and `SUPABASE_KEY` are configured, run the same command without `--dry-run`. It checks `trace_id` in `trade_results` before inserting and marks each outbox row only after a successful response. A failed delivery remains pending for the next run. Run this from a single scheduled worker. The command does not affect fills or account state.

On the Pi, keep these values in `/home/thetopham/.config/tradingview-bot-sim-results.env` with mode `0600`. Install `deploy/tradingview-bot-sim-results.service` and `.timer` to publish pending rows each minute. The n8n Supabase credential can supply the local instance's URL and service key; do not commit either value. Verify a published result by its `sim:<account>:<generation>:<trade>` trace ID. A manually submitted smoke trade has no AI decision ID, so it appears in `trade_results` but may not join into the `ai_trade_feed` view.

## Current boundary and next checks

- The v2 ledger is authoritative for fills, fees, positions, trailing loss, and pass/fail events. The bridge renders ProjectX-shaped read views for the old dashboard/position manager without making a broker call.
- Closed simulated trades are queued locally in `sim_result_outbox` and exposed through `/sim/results`. The separate publisher delivers them to local Supabase with an idempotent trace ID. The Pi's current manual smoke trade was verified in `trade_results`; it has no AI decision ID, so the `ai_trade_feed` join does not include it.
- The 30m feed is connected and normalizes the post-insert receipt timestamp into a closed bar. Epsilon is the only account with a configured ProDex overseer. The 5m and 15m variants still need their feed workflows connected and their own decision routes; otherwise they do not advance.
- Keep the n8n header secret in its encrypted credential and the Pi environment file, never in exported workflow JSON. The original `tradingview-bot` service remains inactive; only `tradingview-bot-sim` runs.
- The Pi's legacy `.env` points at an obsolete Supabase host. The result publisher uses a separate private environment file with the credential from the currently working n8n Supabase connection.
