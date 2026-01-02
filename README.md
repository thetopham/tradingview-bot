# TradingView ProjectX Bot (AI Day Trader Overseer)

This project runs a lightweight execution and observability layer for an “AI day trader” workflow. It receives TradingView webhooks (or scheduled triggers), routes them through an AI overseer, executes trades via TopstepX, captures broker events, and logs results to Supabase for research and monitoring.

- **Webhook intake**: TradingView or n8n webhook (`/webhook`) sends signals that include a shared secret and trade context.
- **AI overseer**: Position/risk context is passed to an AI endpoint (n8n workflow using vision LLMs and chart-img screenshots) to approve or adjust the trade.
- **Execution**: Approved signals are executed via ProjectX / TopstepX API.
- **Monitoring**: A SignalR listener records broker events and writes trade results to Supabase. A dashboard surfaces combined AI + PnL data.
- **Scheduling**: APScheduler can trigger recurring webhooks (5m by default) to keep the loop active.

## Required subscriptions and services

These accounts/keys are required for a production deployment:

- **TopstepX Combine trading account**: Live/Combine account for execution plus **TopstepX API access** (`PROJECTX_BASE_URL`, username, API key).
- **TradingView with CME data**: Paid CME data plan and the ability to send webhooks to your bot host; alerts should include the `secret` and trade context.
- **AI workflow endpoint**: n8n (or similar) hosting for the AI overseer, with access to LLMs and **chart-img** (TradingView screenshot API) for chart context. The sample workflow in `GPT ai trading overseer - simple.json` expects Supabase storage for chart images and chart analysis tables.
- **Supabase project**: URL + service role key for logging AI decisions (`ai_trading_log`), trade results (`trade_results`), and the derived `ai_trade_feed` view. Storage bucket access is also needed if you upload logs or chart images.
- **Linux host with systemd**: A persistent host (Ubuntu/Debian recommended) where the bot, SignalR listener, and schedulers can run under a `systemd` service and optional timers/cron jobs.
- **Python environment**: Python 3.10+ with the packages in `requirements.txt` installed inside a virtual environment.

## Repository map

- `tradingview_projectx_bot.py` – Flask app entrypoint, webhook handler, AI routing, scheduler bootstrap, SignalR listener launch, and dashboard registration.【F:tradingview_projectx_bot.py†L1-L98】【F:tradingview_projectx_bot.py†L127-L176】
- `scheduler.py` – APScheduler setup for recurring webhooks (5-minute cron trigger).【F:scheduler.py†L1-L34】
- `strategies.py` – Execution strategies (currently `simple`).
- `api.py` – TopstepX REST helpers, Supabase logging helpers, and contract utilities.【F:api.py†L1-L73】【F:api.py†L186-L259】
- `auth.py` – TopstepX authentication and token refresh logic.【F:auth.py†L1-L52】
- `position_manager.py` – Builds position/account context for AI.
- `signalr_listener.py` – Broker event listener that logs close events to Supabase.
- `dashboard.py` + `templates/dashboard.html` – API + UI for the merged AI/trade feed.
- `upload_botlog.py` – Utility to upload and prune log files in Supabase Storage.【F:upload_botlog.py†L1-L73】
- `logging_config.py` – File/console logging setup (logs default to `/tmp/tradingview_projectx_bot.log`).
- `env.example` – Starter `.env` with required keys.

## Configuration (.env)

Set the following environment variables (see `env.example` for a template):

- **Bot + API**: `TV_PORT`, `WEBHOOK_SECRET`, `PROJECTX_BASE_URL`, `PROJECTX_USERNAME`, `PROJECTX_API_KEY`.
- **AI**: `N8N_AI_URL` (and optional `N8N_AI_URL2`).
- **Accounts**: `ACCOUNT_<NAME>=<TOPSTEP_ACCOUNT_ID>` for each account (e.g., `ACCOUNT_beta=123456`). The first account becomes the default.
- **Supabase**: `SUPABASE_URL`, `SUPABASE_KEY` (service role for inserts/updates).
- **Execution controls**: `OVERRIDE_CONTRACT_ID` (defaults to `CON.F.US.MES.H26`), `STOP_LOSS_POINTS`, `TP_POINTS` (comma-separated), `TICKS_PER_POINT`, `DAILY_PROFIT_TARGET`, `MAX_DAILY_LOSS`, `MAX_CONSECUTIVE_LOSSES`.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env  # then fill in secrets
python tradingview_projectx_bot.py
```

Visit `http://localhost:<TV_PORT>/dashboard` for the dashboard and `http://localhost:<TV_PORT>/healthz` for a health check.【F:tradingview_projectx_bot.py†L29-L57】

## Deployment on Linux (systemd)

1. Create a dedicated user and working directory, then clone the repo and set up the virtual environment under that user.
2. Place your `.env` in the project root (ensure it is readable by the service account).
3. Create a `systemd` unit to keep the bot online:

`/etc/systemd/system/tradingview_bot.service`
```ini
[Unit]
Description=TradingView ProjectX Bot
After=network.target

[Service]
User=tradingbot
WorkingDirectory=/opt/tradingview-bot
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/opt/tradingview-bot/.env
ExecStart=/opt/tradingview-bot/.venv/bin/python /opt/tradingview-bot/tradingview_projectx_bot.py
Restart=on-failure
RestartSec=5s
StandardOutput=append:/tmp/tradingview_projectx_bot.log
StandardError=append:/tmp/tradingview_projectx_bot.log

[Install]
WantedBy=multi-user.target
```

Reload and enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable tradingview_bot.service
sudo systemctl start tradingview_bot.service
sudo systemctl status tradingview_bot.service
sudo journalctl -u tradingview_bot.service -f
```

The service runs the Flask app, SignalR listener, and APScheduler in one process, so no extra units are required for those components.【F:tradingview_projectx_bot.py†L127-L176】【F:scheduler.py†L1-L34】

## Timers and cron jobs

- **APScheduler (in-process)**: The bundled 5-minute cron trigger posts to `/webhook` to keep the AI loop active even without TradingView alerts. Adjust the schedule in `scheduler.py` if needed.【F:scheduler.py†L14-L34】
- **Log upload + retention**: Run `upload_botlog.py` daily to sync `/tmp/tradingview_projectx_bot.log*` to Supabase Storage and prune files. Example systemd timer:

`/etc/systemd/system/tradingview_bot_logs.service`
```ini
[Unit]
Description=Upload TradingView bot logs to Supabase
After=network-online.target

[Service]
Type=oneshot
User=tradingbot
WorkingDirectory=/opt/tradingview-bot
EnvironmentFile=/opt/tradingview-bot/.env
ExecStart=/opt/tradingview-bot/.venv/bin/python /opt/tradingview-bot/upload_botlog.py
```

`/etc/systemd/system/tradingview_bot_logs.timer`
```ini
[Unit]
Description=Daily TradingView bot log upload

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

Enable with `sudo systemctl enable --now tradingview_bot_logs.timer`.

You can alternatively schedule other maintenance (backup, report generation) via cron or additional timers using the same pattern.

## TradingView webhook contract

Send POST requests to `/webhook` with JSON like:
```json
{
  "secret": "<WEBHOOK_SECRET>",
  "strategy": "simple",
  "account": "beta",
  "signal": "BUY",  // BUY | SELL | HOLD | FLAT
  "symbol": "CON.F.US.MES.H26",
  "size": 3,
  "alert": "TV alert text",
  "ai_decision_id": "optional-correlation-id"
}
```
The bot validates the secret, runs AI oversight when configured, and dispatches the `simple` strategy (other strategies will be rejected).【F:tradingview_projectx_bot.py†L59-L126】【F:tradingview_projectx_bot.py†L144-L175】 A `signal` of `FLAT` will flatten the contract immediately.【F:tradingview_projectx_bot.py†L86-L109】

## Supabase schema

- `ai_trading_log`: AI decisions and rationale (account, symbol, signal, size, reason, chart URLs).
- `trade_results`: Execution outcomes captured from SignalR close events (entry/exit, pnl, raw trades, trace/session IDs).
- `ai_trade_feed`: View that merges AI intent with realized results for dashboard display.

Ensure the Supabase key has insert/update permissions for these tables and Storage buckets used by chart-img and log uploads.

## Logging and observability

- Logs stream to `/tmp/tradingview_projectx_bot.log` (see `logging_config.py`).
- Missing trade results fall back to `/tmp/trade_results_missing.jsonl` and `/tmp/trade_results_fallback.jsonl` for troubleshooting.
- Health endpoint: `GET /healthz` returns `{status: "ok"}` with a timestamp for uptime probes.【F:tradingview_projectx_bot.py†L29-L57】

## Safety notes

This code can execute real orders. Test with a TopstepX sim/Combine account first, configure risk limits, and verify webhook secrets before trading live.
