# Pi simulator services

This describes the running **simulation** service set, checked on 2026-09-24. The old `tradingview_bot.service`, its restart and log timers, and the ProjectX/SignalR path are masked. Do not start them to operate the simulator. For dashboard use, see the [user guide](https://github.com/thetopham/tradingview-bot-v2/blob/main/documentation/user-guide.md).

| Unit | Job | Current schedule |
| --- | --- | --- |
| `tradingview-bot-sim.service` | Flask bridge and authenticated dashboard, port 5001 | Long-running, enabled |
| `tradingview-bot-sim-decisions.timer` | Checks cached 5m/15m/30m candles and calls each configured overseer once per fresh candle | Every minute at about `:05` |
| `tradingview-bot-sim-results.timer` | Retries closed-trade delivery from local outbox to Supabase | Every minute |
| `tvbot-strategy-farm.timer` | Read-only five-minute indicator screen and immutable report | Weekdays near 09:00 America/Denver |

n8n runs in Docker on the Pi, with a local health endpoint on port 5678. The one-minute n8n datafeed advances broker fills and stops directly through the private Flask `/sim/feed` route; it does not call ProDex. The decision timer only calls ProDex when a fresh strategy candle is available. The strategy farm is separate from broker orders.

The bridge checkout is `/home/thetopham/tradingview-bot-sim`; the v2 checkout is `/home/thetopham/tradingview-bot-v2`. The service loads `/home/thetopham/.config/tradingview-bot-sim.env` privately and points at the v2 ledger `data/sim_broker_local.sqlite`. Result publishing uses its own private environment file. Never print or commit either file's contents. The published dashboard host exposes only `/sim/dashboard` and `/sim/dashboard/data`; broker routes remain private.

## Health and logs

```bash
systemctl is-active tradingview-bot-sim.service tradingview-bot-sim-decisions.timer tradingview-bot-sim-results.timer tvbot-strategy-farm.timer
systemctl list-timers --all | grep -E 'tradingview-bot-sim|tvbot-strategy-farm'
curl -fsS http://127.0.0.1:5001/healthz
curl -fsS http://127.0.0.1:5678/healthz
journalctl -u tradingview-bot-sim.service -n 100 --no-pager
journalctl -u tradingview-bot-sim-decisions.service -n 100 --no-pager
journalctl -u tradingview-bot-sim-results.service -n 100 --no-pager
journalctl -u tvbot-strategy-farm.service -n 100 --no-pager
```

At the 2026-09-24 check, the simulator service and three timers were active; both local health endpoints returned HTTP 200. A stopped market can make the last one-minute candle old without indicating an outage. During market hours, inspect the feed workflow and timestamp if the candle stops advancing.

## Updating a checkout

Pull reviewed code in the relevant Pi checkout. Documentation-only changes need **no service restart**. For Python bridge changes, verify that the v2 package installed in the bridge virtual environment still resolves to the v2 checkout before restarting `tradingview-bot-sim.service`. For a new account, initialize its profile in the v2 ledger, set a private `N8N_OVERSEER_URL_<ACCOUNT>` route, then restart the bridge so it reloads the account map. See the [bridge runbook](../docs/sim-broker-bridge.md) for the full sequence. Do not reset the live ledger or start the masked ProjectX units.

The historical ProjectX service design is preserved in Git history and the [original system reference](README.md); it is not an operating procedure for this Pi setup.
