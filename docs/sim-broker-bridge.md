# Plugging the v2 simulator into the legacy bot

For dashboard use and a plain-language overview, start with the [user guide](https://github.com/thetopham/tradingview-bot-v2/blob/main/documentation/user-guide.md). This runbook describes the Pi simulator bridge; the historical ProjectX service is masked.

The legacy bot's original broker boundary was `api.py` plus the SignalR listener. `BROKER_MODE=sim` uses the persistent `tradingview-bot-v2` SQLite ledger for account, position, bracket order, and trade-fill views. It accepts decisions through the existing `/webhook` route. In this mode, startup does not authenticate to ProjectX, launch SignalR, or start the legacy broker scheduler. Direct ProjectX API calls are rejected before authentication.

The original ProjectX service on the Pi remains disabled. The separate `tradingview-bot-sim` bridge is active and uses only the simulated broker.

## Configuration

Install `requirements-sim.txt` (or install the local v2 checkout into the same environment). Point the bridge to an **already initialized** v2 database:

On the Pi, the bridge virtual environment uses an editable install of `/home/thetopham/tradingview-bot-v2`. After pulling v2 changes, verify that `tvbot_v2.simulate.ledger.__file__` points into that checkout. A stale installed wheel can leave the bridge running old ledger code even after both repositories are updated. The systemd override uses two Gunicorn workers and a 120-second timeout so one-minute bars can continue while a 30-minute ProDex request is in flight.

```text
BROKER_MODE=sim
SIM_BROKER_DB=/home/thetopham/tradingview-bot-v2/data/sim_broker_local.sqlite
TV_PORT=5001
WEBHOOK_SECRET=<local secret>
DASHBOARD_PASSWORD=<local password>
SIM_MAX_BAR_LAG_SECONDS=600
SIM_DECISION_SOURCE=scheduler
```

Simulation mode reads account names from the v2 ledger at startup. It assigns stable compatibility account IDs from SQLite row IDs, starting at 900001. Add accounts with the v2 `init --portfolio` command, then restart this legacy bridge to refresh its account map. Profile names must be lowercase for legacy webhook routing. New accounts can use `N8N_OVERSEER_URL_<ACCOUNT>`; the historical alpha through practice URL variables continue to work. `PROJECTX_*` settings are unused in sim mode.

`SIM_MAX_BAR_LAG_SECONDS` rejects stale bars in forward operation. Set it to `0` only for an isolated replay or test database.

For the Pi's separate decision scheduler, set `SIM_DECISION_SOURCE=scheduler` and install `deploy/tradingview-bot-sim-decisions.service` and `.timer`. The 5-minute, 15-minute, and 30-minute n8n datafeeds forward their saved candles; `/sim/feed` caches them without calling the AI. Each timer run checks for a fresh cached candle and invokes each configured overseer once per account and candle. Eligible accounts run concurrently so a 30-minute boundary can handle all ten without serial model delays. The resulting decision enters the local broker and is filled from the next eligible one-minute bar. This setting avoids running both the feed and scheduler as decision triggers. Accounts without configured overseer URLs are skipped. The old live-broker scheduler remains disabled.

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

For a profile with 30-minute decisions and 1-minute execution, the 30-minute route records the ProDex decision separately and queues it for the first eligible one-minute bar **whose open follows the response time**. This prevents a delayed model response from receiving an earlier candle's open price. The running `epsilon` and `epsilon_vision` accounts both use this one-minute execution path. `epsilon_1m` is an older optional example profile for isolated experiments, not an additional live Pi account.

The route fans the bar out to **every** configured account with the matching timeframe. Each account can call its own n8n overseer URL. For isolated replay, an optional `decisions` object may contain account-keyed decisions; production feed rows have none. The response lists account snapshots and any per-account errors. Retrying a bar is safe.

On the Pi, the published `datafeed_5m`, `datafeed_15m`, and `datafeed_30m` workflows forward their inserted Supabase rows to this route with a dedicated n8n Header Auth credential. The bridge binds to Pi loopback and the private n8n Docker gateway at port 5001. Alpha through epsilon call their distinct numeric ProDex workflows. The paired `*_vision` accounts call separate workflows that retain each strategy's prompt and add a Chart-Img screenshot before the decision. Chart uploads and permanent Google Cloud Storage URL logging run after the broker response. See [the paired experiment](paired-chart-vision-experiment.md).

### Live bracket settings

The running `tradingview-bot-sim` service reads its ledger path from `SIM_BROKER_DB` in its private environment file. On 2026-09-23 this is `data/sim_broker_local.sqlite`; `data/sim_broker.sqlite` is an older, inactive ledger with different settings. Check the service's configured path before reading account profiles or balances.

All ten active accounts currently execute on the **1-minute** feed, while their decisions arrive on 5-minute (alpha, beta, gamma, and their vision pairs), 15-minute (delta and its pair), or 30-minute (epsilon and its pair) candles. All ten have the same choices below; account-specific brackets are supported by the v2 profile format but have not yet been assigned to these live accounts.

| Decision `size` | MES contracts | Stop | Target | Gross stop / target |
| --- | ---: | ---: | ---: | ---: |
| 1 | 1 | 24 ticks (6 points) | 48 ticks (12 points) | −$30 / +$60 |
| 2 | 2 | 12 ticks (3 points) | 24 ticks (6 points) | −$30 / +$60 |
| 3 | 3 | 8 ticks (2 points) | 16 ticks (4 points) | −$30 / +$60 |

One MES tick is 0.25 index points and $1.25 per contract. The broker fills an eligible decision at the next 1-minute open with one tick of entry slippage, then sets stop and target from that actual entry price. Each following 1-minute OHLC bar can trigger an exit. If both levels are touched in the same bar, the stop wins. Stops can fill beyond their level on a gap and incur one tick of adverse slippage. Fees are $1.22 round turn per MES contract. The maximum-loss rule can also close a position independently of its bracket. These costs and rules make realized P&L differ from the gross table.

The Pi's n8n container needs its persistent `nodes/node_modules/@openai` directory mounted read-only into `/usr/local/lib/node_modules/n8n/node_modules/@openai`. ProDex's SDK is dynamically imported from n8n's install tree; without that mount it fails after a container replacement. Keep this mount in the Pi's local `n8n-docker-caddy/docker-compose.yml` alongside the regular `n8n_data` volume. Check a direct overseer response after n8n upgrades or recreations before allowing new automatic decisions.

`GET /sim/events?after_id=0&limit=100` returns normalized ledger events and a `next_cursor` for the next poll. Pass `X-Webhook-Secret` in the request header. `GET /sim/results?after_id=0&limit=100` returns durable, ProjectX-shaped `trade_results` payloads built from closed v2 trades. The outbox is backfilled from the ledger when the adapter starts and uses the v2 trade ID plus account and generation as its unique key. This replaces SignalR's position-close trigger without coupling simulated execution to delivery.

To check how many results are awaiting Supabase, set `SIM_BROKER_DB` and run:

```bash
.venv/bin/python scripts/publish_sim_results.py --dry-run
```

Once a valid `SUPABASE_URL` and `SUPABASE_KEY` are configured, run the same command without `--dry-run`. It checks `trace_id` in `trade_results` before inserting and marks each outbox row only after a successful response. A failed delivery remains pending for the next run. Run this from a single scheduled worker. The command does not affect fills or account state.

On the Pi, keep these values in `/home/thetopham/.config/tradingview-bot-sim-results.env` with mode `0600`. Install `deploy/tradingview-bot-sim-results.service` and `.timer` to publish pending rows each minute. The n8n Supabase credential can supply the local instance's URL and service key; do not commit either value. Verify a published result by its `sim:<account>:<generation>:<trade>` trace ID. A manually submitted smoke trade has no AI decision ID, so it appears in `trade_results` but may not join into the `ai_trade_feed` view.

The numeric ProDex workflows insert successful decisions into Supabase `ai_trading_log`. The `ai_trade_feed` table is refreshed by database triggers and combines these decisions with `trade_results` after a position closes; HOLD decisions therefore appear without trade P&L. On 2026-09-23 the restored Supabase database had `ai_decision_id` above 33,000 while its shared `documents_id_seq` was at 90. New decisions inserted at low IDs and disappeared from a descending-ID view. The sequence was advanced above the maximum ID across all tables that share it. After a future restore, verify both `MAX(ai_decision_id)` and the sequence's `last_value` before diagnosing missing logs. If the sequence is behind, run `scripts/repair_supabase_shared_sequence.sql` against the local database; it locks the tables while taking a consistent maximum. The feed refresh function also now falls back to the decision's `prompt_version` when a trade result has none.

## Current boundary and next checks

- The v2 ledger is authoritative for fills, fees, positions, trailing loss, and pass/fail events. The bridge renders ProjectX-shaped read views for the old dashboard/position manager without making a broker call.
- Closed simulated trades are queued locally in `sim_result_outbox` and exposed through `/sim/results`. The separate publisher delivers them to local Supabase with an idempotent trace ID. The Pi's current manual smoke trade was verified in `trade_results`; it has no AI decision ID, so the `ai_trade_feed` join does not include it.
- The 5m, 15m, and 30m decision feeds are connected. The active one-minute feed advances fills and risk for all ten current accounts. The paired image workflows were separately checked for binary image delivery; see [the paired experiment](paired-chart-vision-experiment.md).
- Keep the n8n header secret in its encrypted credential and the Pi environment file, never in exported workflow JSON. The original `tradingview-bot` service remains inactive; only `tradingview-bot-sim` runs.
- The Pi's legacy `.env` points at an obsolete Supabase host. The result publisher uses a separate private environment file with the credential from the currently working n8n Supabase connection.

## Simulated account dashboard

`GET /sim/dashboard` shows every account in the v2 ledger, including new variants after the bridge restarts. It displays current equity, realized and open P&L, remaining maximum-loss room, profit-goal progress, position brackets, the latest decision, the last execution candle, and up to 30 recently closed trades. The page refreshes every 30 seconds. `GET /sim/dashboard/data` returns the same read-only snapshot as JSON. Neither endpoint queries ProjectX or Supabase. The existing `/dashboard` remains the legacy Supabase trade feed.

Both new routes require `DASHBOARD_PASSWORD`; if it is unset they return 503. They should be published only over HTTPS or reached through an SSH tunnel. For the Pi tunnel, use `ssh -L 5001:127.0.0.1:5001 pi` and open `http://localhost:5001/sim/dashboard`. Use the dashboard password from the Pi's private simulator environment file. Browser user name is `dashboard`.

The page reports the last one-minute candle even during market closure. A large age after the market closes is expected; compare the timestamp with market hours before treating it as a feed outage. Equity on open trades is marked from the most recent candle close.

On the Pi, `https://sim.thetopham.com/sim/dashboard` is routed through the existing Cloudflare tunnel to the private Flask port. The tunnel ingress rule must match only `^/sim/dashboard(/data)?$`, followed by a host-specific `http_status:404` rule. This prevents the public hostname from reaching broker order, feed, and webhook routes. Keep the n8n and alerts ingress entries intact. The authenticated dashboard uses Basic Auth over HTTPS; do not put its password in a URL, repository, or screenshot.
