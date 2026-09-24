# TradingView bot: simulator bridge and dashboard

This repository runs the **Flask bridge** between TradingView/n8n and the [TradingView Bot v2 simulated broker](https://github.com/thetopham/tradingview-bot-v2). It retains the original ProjectX adapter for historical reference, but the Pi's active `tradingview-bot-sim.service` uses `BROKER_MODE=sim`. The original `tradingview_bot.service` is masked. The running setup places no real broker orders.

**Start here:** [User guide](https://github.com/thetopham/tradingview-bot-v2/blob/main/documentation/user-guide.md) · [Simulator bridge runbook](docs/sim-broker-bridge.md) · [Numeric versus chart trial](docs/paired-chart-vision-experiment.md) · [Pi services](documentation/pi-systemd.md)

## Use the running simulator

Open the [authenticated account dashboard](https://sim.thetopham.com/sim/dashboard). The browser username is `dashboard`; use the password set in the Pi's private simulator environment. The dashboard shows all registered accounts, positions, brackets, P&L, loss room, recent decisions, and closed trades. It is read-only.

The Pi currently runs five numeric ProDex strategies and five corresponding chart-image variants. Alpha, beta, and gamma decide on five-minute candles; delta on fifteen-minute candles; epsilon on thirty-minute candles. All ten receive the same closed **one-minute MES feed for fills, stops, targets, and risk checks**. The one-minute feed does not call ProDex. New independent demo accounts can be added without a software count limit, provided each has a profile and an overseer route.

```text
TradingView/n8n 1m feed -> POST /sim/feed -> v2 broker ledger
TradingView/n8n 5m/15m/30m feed -> cached candle -> decision timer -> n8n ProDex
ProDex decision -> queued broker order -> next eligible 1m open
closed broker trade -> durable outbox -> Supabase trade_results
ProDex decision -> Supabase ai_trading_log -> ai_trade_feed
```

The bridge exposes ProjectX-shaped account, order, position, trade, and broker-event views for legacy callers while reading and writing only the local v2 ledger. `/sim/broker-events` is an optional authenticated polling view of ledger events, not a SignalR server. Broker fills, brackets, and trade-close detection do not depend on polling this endpoint. The public `sim.thetopham.com` tunnel is restricted to the authenticated dashboard routes, not the order or feed endpoints.

The active Pi still uses short timer checks for other jobs: the decision timer looks for a newly cached strategy candle, the results timer retries delivery from the durable outbox to Supabase, and the browser refreshes the dashboard every 30 seconds. The n8n one-minute feed **pushes** each closed candle to the broker; there is no broker-status poll to discover fills.

## Repository roles

- `tradingview_projectx_bot.py`: Flask webhook, simulation feed/decision routes, and bridge setup.
- `brokers/sim_adapter.py`, `brokers/sim_decision_feed.py`, and `brokers/sim_results.py`: v2 ledger compatibility, cached strategy candles, and result delivery.
- `templates/` and dashboard modules: simulated account and legacy Supabase views.
- `deploy/` and `scripts/`: Pi service units, decision timer, and result publisher.
- `n8n/`: exported workflows and historical examples. Active n8n definitions and credentials live in the Pi instance; an exported JSON file is not proof of what is currently published.
- `documentation/README.md`: historical ProjectX system reference. It is not the current simulator operating guide.

The active Pi checkout is `/home/thetopham/tradingview-bot-sim`. Its simulator configuration is in a private environment file. The active v2 ledger is `/home/thetopham/tradingview-bot-v2/data/sim_broker_local.sqlite`. Use a separate database for local tests or replays. See the [bridge runbook](docs/sim-broker-bridge.md) for setup, account routing, one-minute timestamp requirements, brackets, durable result publishing, and dashboard access.

## Historical implementation

The [archival ProjectX/SignalR release](https://github.com/thetopham/tradingview-bot/releases/tag/legacy-projectx-signalr-final) preserves the exact last main-branch source commit before the simulator bridge. The [original system documentation](documentation/README.md), [coupling audit](docs/trading-bot-coupling-audit.md), and [compatibility design](docs/broker-compatibility-layer.md) preserve how the old bot worked and how migration was planned. These are historical references. Do not start the old ProjectX service to operate the simulator.

## Security

Keep webhook secrets, Supabase keys, Chart-Img sessions, OAuth credentials, and dashboard passwords out of Git and workflow exports. The v2 broker is a research simulator; its balances and fills are not Topstep balances or executions.
