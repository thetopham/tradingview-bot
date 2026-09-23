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

`GET /sim/events?after_id=0&limit=100` returns normalized ledger events and a `next_cursor` for the next poll. Pass `X-Webhook-Secret` in the request header. A future result dispatcher can consume `trade_closed` events from this endpoint, replacing the old SignalR close trigger without coupling the simulator to the transport.

## Current boundary and next checks

- The v2 ledger is authoritative for fills, fees, positions, trailing loss, and pass/fail events. The bridge renders ProjectX-shaped read views for the old dashboard/position manager without making a broker call.
- Closed simulated trades are **not yet copied to the legacy `trade_results` Supabase table**. The old repo's dry-run proof established the payload shape, and the bridge can now render compatible entry and exit fills. The next dispatcher must publish each closed trade once, keep its v2 trade ID, and reconcile against the existing `ai_trade_feed` view.
- The live n8n workflow currently pulls its own feed. It has not been changed to include a canonical closed bar in the webhook response. Until it does, submit only test envelopes to the local bridge. A missing bar is rejected rather than guessed from wall-clock time or a stale quote.
- The Pi's legacy `.env` points at an obsolete Supabase host. Local Supabase at `192.168.0.35:8000` is reachable but requires a valid key. Do not reuse the old key or start the old scheduler for this bridge.
