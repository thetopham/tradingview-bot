# Project Setup and Operations Guide

This guide documents the full set of services, subscriptions, and host requirements for running the TradingView ProjectX bot in production. It complements the quick start in the root README with deployment-grade details (accounts, secrets, systemd units, and scheduled maintenance).

## Required subscriptions and services

| Area | What is needed | Why it is required |
| --- | --- | --- |
| Trading account | **TopstepX Combine account** with live trading credentials | The bot executes orders and listens to fills via ProjectX/TopstepX APIs. |
| API access | **TopstepX API credentials** (username, API key) | Used by `auth.py`/`api.py` to authenticate and submit trades. |
| Market data & alerts | **TradingView account with CME data** and custom webhook alerts | Webhook alerts trigger the bot; CME futures data keeps signals aligned with MES/ES contracts. |
| AI workflow | **n8n instance** reachable by the bot | The `/webhook` route forwards context to an n8n AI flow defined in `GPT ai trading overseer - simple.json`. |
| Chart capture | **chart-img** API key and TradingView session headers | The n8n flow pulls annotated chart images for the AI prompt. |
| Database & storage | **Supabase project** (Postgres + Storage) | Stores AI decisions (`ai_trading_log`), trade outcomes (`trade_results`), and uploaded bot logs (bucket `botlogs`). |
| Hosting | **Linux server with systemd** (Ubuntu/Debian/RHEL) | Runs the Flask bot, SignalR listener, APScheduler, and maintenance timers. |

## Host preparation

1. Install system dependencies: Python 3.11+, `python3-venv`, `systemd`, `curl`, and `git`.
2. Clone the repo to `/opt/tradingview-bot` (or another managed path):
   ```bash
   sudo mkdir -p /opt/tradingview-bot
   sudo chown $(whoami):$(whoami) /opt/tradingview-bot
   git clone https://.../tradingview-bot.git /opt/tradingview-bot
   cd /opt/tradingview-bot
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Copy `env.example` to `.env` and fill in secrets (see below). Keep the file readable by the systemd service user only.
4. (Optional) Configure log rotation for `/tmp/tradingview_projectx_bot.log*` if you prefer system-level logrotate.

## Environment variables

Create `/opt/tradingview-bot/.env` with at least:

- **Core runtime**: `TV_PORT`, `WEBHOOK_SECRET`, `PROJECTX_BASE_URL`, `PROJECTX_USERNAME`, `PROJECTX_API_KEY`.
- **Accounts**: one or more `ACCOUNT_<NAME>=<NUM>` entries (e.g., `ACCOUNT_beta=12345`). The first entry becomes `DEFAULT_ACCOUNT`.
- **AI routing**: `N8N_AI_URL` (and `N8N_AI_URL2` if used by the flow). These correspond to the n8n webhook URLs that process the prompt.
- **Supabase**: `SUPABASE_URL`, `SUPABASE_KEY` for logging and storage access.
- **Risk and contract overrides**: `DAILY_PROFIT_TARGET`, `MAX_DAILY_LOSS`, `MAX_CONSECUTIVE_LOSSES`, `STOP_LOSS_POINTS`, `TP_POINTS`, `TICKS_PER_POINT`, `OVERRIDE_CONTRACT_ID`.
- **Optional**: `WEBHOOK` for Slack/Teams alerts if configured by downstream tools.

> The n8n workflow (`GPT ai trading overseer - simple.json`) expects `chart-img` headers (`tradingview-session-id`, `tradingview-session-id-sign`) to be configured inside n8n credentials. Keep those secrets with the n8n deployment, not in this repo.

## Runtime services (systemd)

Place unit files in `/etc/systemd/system/` and reload daemon state after creating them.

### tradingview-bot service

Runs the Flask webhook server, APScheduler, and SignalR listener in a single process.

`/etc/systemd/system/tradingview_bot.service`:

```ini
[Unit]
Description=TradingView ProjectX Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=bot
WorkingDirectory=/opt/tradingview-bot
EnvironmentFile=/opt/tradingview-bot/.env
ExecStart=/bin/bash -lc 'source .venv/bin/activate && python tradingview_projectx_bot.py'
Restart=always
RestartSec=5
StandardOutput=append:/tmp/tradingview_projectx_bot.log
StandardError=append:/tmp/tradingview_projectx_bot.log

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable tradingview_bot.service
sudo systemctl start tradingview_bot.service
sudo systemctl status tradingview_bot.service
```

### Log upload & cleanup timer

`upload_botlog.py` syncs `/tmp/tradingview_projectx_bot.log*` files to the Supabase storage bucket (`botlogs`) and purges entries older than `DAYS_TO_KEEP` (default 30 days). Install it as a systemd timer (preferred over cron for dependency ordering).

`/etc/systemd/system/upload_botlog.service`:

```ini
[Unit]
Description=Upload TradingView bot logs to Supabase
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=bot
WorkingDirectory=/opt/tradingview-bot
EnvironmentFile=/opt/tradingview-bot/.env
ExecStart=/bin/bash -lc 'source .venv/bin/activate && python upload_botlog.py'
StandardOutput=append:/tmp/tradingview_projectx_bot.log
StandardError=append:/tmp/tradingview_projectx_bot.log
```

`/etc/systemd/system/upload_botlog.timer`:

```ini
[Unit]
Description=Daily upload of TradingView bot logs to Supabase

[Timer]
OnCalendar=*-*-* 23:55:00
Persistent=true
Unit=upload_botlog.service

[Install]
WantedBy=timers.target
```

Enable the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now upload_botlog.timer
sudo systemctl list-timers --all | grep upload_botlog
```

> If you prefer cron, you can instead add `55 23 * * * /bin/bash -lc 'cd /opt/tradingview-bot && source .venv/bin/activate && python upload_botlog.py >> /tmp/tradingview_projectx_bot.log 2>&1'` to the service user’s crontab.

## Operational checklist

- **Health checks**: `/healthz` returns `{"status": "ok"}`; point uptime monitoring at `http://<host>:<TV_PORT>/healthz`.
- **Webhook secrets**: ensure TradingView alerts send `secret` that matches `WEBHOOK_SECRET`; n8n should forward the same value if used as a relay.
- **Contract handling**: `OVERRIDE_CONTRACT_ID` forces a specific MES/ES contract; otherwise `api.get_contract` maps symbols dynamically.
- **Risk windows**: trades are blocked during `GET_FLAT_START`–`GET_FLAT_END` (15:07–17:00 CT) via `in_get_flat`.
- **Scheduler**: APScheduler triggers a 5-minute heartbeat webhook (`scheduler.py`). Disable or adjust the CronTrigger values if your strategy should remain event-driven only.
- **Data retention**: log retention defaults to 30 days (configurable in `upload_botlog.py`). Update `DAYS_TO_KEEP` if your storage policy differs.

## Data flow overview

1. TradingView (or n8n) posts to `/webhook` with `secret`, `strategy`, `account`, `signal`, `symbol`, `size`, and `alert` fields.
2. The bot enriches the request with positions/risk context, calls the AI endpoint (`ai_trade_decision`), and validates the AI’s returned signal.
3. `run_simple` executes the order via ProjectX/TopstepX APIs.
4. `signalr_listener.py` captures fill/close events and writes `trade_results` to Supabase; decisions are logged to `ai_trading_log`.
5. `dashboard.py` serves `/dashboard` for merged telemetry; the dataset can be exported to train future models.

## Troubleshooting

- Inspect `/tmp/tradingview_projectx_bot.log` and `/tmp/trade_results_missing.jsonl` for incomplete close events.
- Validate API access with a manual `authenticate()` run or small `get_contract` query inside a Python shell.
- If the bot does not receive webhooks, confirm firewall rules and that `TV_PORT` is reachable.
- For Supabase upload issues, run `python upload_botlog.py` manually with the virtual environment activated to surface credential errors.

