# ProjectX TradingView Bot – Deployment & Operations Guide

This document collects every requirement needed to run the TradingView ProjectX bot in production: third-party subscriptions, infrastructure, Linux services, and operational runbooks.

## Required subscriptions and external services

| Service | Purpose | Minimum entitlement |
| --- | --- | --- |
| **TopstepX Combine account** | Source account for order routing and balances. | An active Combine or funded account per environment (e.g., `beta`, `epsilon`) with live trading enabled. |
| **TopstepX API access** | Bot authentication for REST (`api.py`) and SignalR (`signalr_listener.py`). | Valid credentials accepted by `auth.py` (username/password or refresh tokens) and websocket access to `wss://rtc.topstepx.com/hubs/user`. |
| **TradingView with CME data** | Sends strategy webhooks and provides CME symbols (e.g., MES). | TradingView plan that supports webhooks **and** CME data subscription. Each alert must include the shared `WEBHOOK_SECRET`. |
| **Supabase** | Primary datastore for AI decisions (`ai_trading_log`) and results (`trade_results`, `ai_trade_feed`). | Supabase project URL + Service Role key with insert/update permission on the three tables. Configure buckets if you mirror chart images. |
| **chart-img** | Generates TradingView chart snapshots for LLM review and dashboard display. | `chart-img` API key with TradingView layout storage enabled. The n8n workflow uses the `tradingview/layout-chart` endpoint. |
| **Linux host with systemd** | Runtime for the Flask API, SignalR listener, and scheduler. | Ubuntu/Debian/CentOS with `python3`, `systemd`, and outbound HTTPS/WSS connectivity to TradingView, TopstepX, Supabase, and chart-img. Cron is optional for housekeeping. |

## Runtime topology

- **Webhook/API**: `tradingview_projectx_bot.py` exposes `/webhook` (signal intake), `/dashboard` (UI), and REST helpers (`api.py`).
- **Strategy execution**: `strategies.py` executes signals; `position_manager.py` builds account context.
- **Result ingestion**: `signalr_listener.py` maintains a websocket to TopstepX and writes PnL to Supabase.
- **Scheduler**: `scheduler.py` runs optional heartbeat webhooks every 5 minutes (APScheduler cron trigger).
- **Observability**: `dashboard.py` + `templates/dashboard.html` render merged AI/trade feeds; logs are emitted to stdout/systemd journal.

## Environment and secrets

Create `/workspace/tradingview-bot/.env` (or export variables in the unit files). Minimum keys:

- `TV_PORT` – Flask port exposed by `tradingview_projectx_bot.py`.
- `WEBHOOK_SECRET` – Shared secret for TradingView alerts.
- `SUPABASE_URL` and `SUPABASE_KEY` – Service Role recommended.
- `N8N_AI_URL` – AI decision webhook URL (e.g., n8n vision LLM endpoint).
- `ACCOUNT_<name>` – One per TopstepX account (e.g., `ACCOUNT_beta=123456`).
- Optional overrides: `OVERRIDE_CONTRACT_ID`, additional logging toggles from `config.py`.

## Systemd units

Place unit files in `/etc/systemd/system/` and reload with `systemctl daemon-reload` after edits.

### tradingview_bot.service (Flask webhook + dashboard)
```ini
[Unit]
Description=TradingView ProjectX Bot API
After=network.target

[Service]
WorkingDirectory=/workspace/tradingview-bot
EnvironmentFile=/workspace/tradingview-bot/.env
ExecStart=/usr/bin/python /workspace/tradingview-bot/tradingview_projectx_bot.py
Restart=on-failure
User=www-data
Group=www-data

[Install]
WantedBy=multi-user.target
```

### tradingview_signalr.service (TopstepX listener)
```ini
[Unit]
Description=TopstepX SignalR Listener
After=network-online.target
Requires=tradingview_bot.service

[Service]
WorkingDirectory=/workspace/tradingview-bot
EnvironmentFile=/workspace/tradingview-bot/.env
ExecStart=/usr/bin/python /workspace/tradingview-bot/signalr_listener.py
Restart=always
User=www-data
Group=www-data

[Install]
WantedBy=multi-user.target
```

### tradingview_scheduler.service (optional APScheduler kick-off)
```ini
[Unit]
Description=APScheduler heartbeat for TradingView ProjectX Bot
After=network.target
Requires=tradingview_bot.service

[Service]
WorkingDirectory=/workspace/tradingview-bot
EnvironmentFile=/workspace/tradingview-bot/.env
ExecStart=/usr/bin/python - <<'PYCODE'
from tradingview_projectx_bot import app
from scheduler import start_scheduler
scheduler = start_scheduler(app)
app.run(host='0.0.0.0', port=int(__import__('os').environ.get('TV_PORT', 5000)))
PYCODE
Restart=on-failure
User=www-data
Group=www-data

[Install]
WantedBy=multi-user.target
```

### Enable and start
```bash
sudo systemctl daemon-reload
sudo systemctl enable tradingview_bot.service tradingview_signalr.service
sudo systemctl start tradingview_bot.service tradingview_signalr.service
sudo systemctl status tradingview_bot.service tradingview_signalr.service
```

If you deploy the APScheduler wrapper separately, enable `tradingview_scheduler.service` as well.

## Cron/timers

- **Log rotation / clean-up (optional)**: configure `/etc/cron.d/tradingview-bot` to purge `/tmp/trade_results_missing.jsonl` and `/tmp/trade_results_fallback.jsonl` daily if they grow large.
- **Health probes**: a simple curl can be scheduled via `systemd` timers to hit `http://localhost:<TV_PORT>/dashboard` and log availability.

Example timer and service pair:
```ini
# /etc/systemd/system/tradingview_health.service
[Unit]
Description=TradingView bot health probe

[Service]
Type=oneshot
ExecStart=/usr/bin/curl -fsS http://localhost:{{TV_PORT}}/dashboard

# /etc/systemd/system/tradingview_health.timer
[Unit]
Description=Run TradingView bot health probe every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=tradingview_health.service

[Install]
WantedBy=timers.target
```

## Deployment steps (fresh host)

1) **Install OS packages**
```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git curl
```

2) **Clone and install**
```bash
git clone https://github.com/.../tradingview-bot.git
cd tradingview-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3) **Configure environment**: copy `env.example` to `.env` and fill values for TradingView, TopstepX, Supabase, and chart-img.

4) **System users/logging**: create a non-root user (e.g., `www-data`), ensure write access to `/tmp/` for fallback logs, and configure journal retention per your ops policy.

5) **Register systemd units**: drop the service/timer files above, `systemctl daemon-reload`, then `enable` + `start`.

6) **Webhook wiring**: in TradingView alerts, set the webhook URL to `http(s)://<host>:<TV_PORT>/webhook` and include `{ "secret": "<WEBHOOK_SECRET>", ... }` payloads.

7) **Supabase schema**: confirm the three tables exist (`ai_trading_log`, `trade_results`, `ai_trade_feed`) and the API key can insert/update them.

8) **Chart snapshots (optional)**: configure chart-img credentials in your n8n workflow so chart URLs populate the dashboard.

## Operations

- **Monitor services**: `systemctl status` and `journalctl -u tradingview_bot.service -f` for live logs.
- **Dashboard**: reachable at `http://<host>:<TV_PORT>/dashboard` for merged AI/trade feeds.
- **SignalR resilience**: `signalr_listener.py` auto-reconstructs metadata for open positions on restart; ensure it starts with the bot.
- **Fail-safe files**: investigate `/tmp/trade_results_missing.jsonl` and `/tmp/trade_results_fallback.jsonl` when Supabase inserts are missing.
- **Security**: restrict inbound ports to TradingView webhook sources; keep `.env` readable only by the service user.

## Reference commands

```bash
# Start/stop
sudo systemctl start tradingview_bot.service
sudo systemctl restart tradingview_bot.service
sudo systemctl stop tradingview_bot.service

# Logs
sudo journalctl -u tradingview_bot.service -f

# Validate webhook endpoint locally
curl -X POST http://localhost:$TV_PORT/webhook \
  -H 'Content-Type: application/json' \
  -d '{"secret":"'$WEBHOOK_SECRET'","signal":"BUY","account":"beta","symbol":"CON.F.US.MES.H26","size":1}'
```
