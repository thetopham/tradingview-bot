# ProjectX Trading Bot – Comprehensive Deployment & Operations Guide

This guide covers end-to-end requirements for running the TradingView ProjectX Bot in production, including paid subscriptions, environment variables, Linux host setup, systemd units/timers, and operational workflows.

## 1. Required subscriptions and accounts

| Area | Why it is needed | Notes |
| --- | --- | --- |
| **TopstepX combine account** | Provides the funded/sim account the bot trades against. Account IDs map to `ACCOUNT_*` env vars used by the webhook handler. | Ensure the account is enabled for API trading and has risk limits configured on the TopstepX side. |
| **TopstepX API access** | Required for authentication (`auth.py`), contract lookup (`api.get_contract`), position search, order placement, and SignalR event streaming. | Obtain API key + username for `PROJECTX_API_KEY` and `PROJECTX_USERNAME`, and confirm the base URL (`PROJECTX_BASE_URL`) matches your region. |
| **TradingView with CME data** | TradingView sends strategy alerts into `/webhook`. CME futures data (e.g., MES/ES) is required to generate valid symbols/alerts. | Configure alerts to include `secret`, `account`, `strategy`, `signal`, `symbol`, and `size` payload fields. |
| **Supabase** | Stores AI decisions (`ai_trading_log`), trade results (`trade_results`), merged feed (`ai_trade_feed`), and hosts log uploads in the `botlogs` storage bucket. | Needs `SUPABASE_URL` and `SUPABASE_KEY` with table + storage permissions. |
| **n8n / AI decision API** | Vision/LLM workflow that reviews context and returns trade decisions consumed by `ai_trade_decision`. | Point `N8N_AI_URL` (and per-account overrides) to the n8n webhook URLs. |
| **chart-img API** | Optional image rendering for dashboards or alert visualizations. | Keep API credentials alongside other secrets if used in custom strategies or dashboards. |
| **Linux host (systemd)** | Long-running services, timers, and log rotation depend on systemd. | Use a recent Debian/Ubuntu/RHEL with Python 3.10+ and outbound HTTPS access. |

## 2. Environment and secrets

Create `/etc/tradingview-bot/.env` (or project `.env`) with the keys below. See `env.example` for the full list. Key items:

- **Network & auth:** `PROJECTX_BASE_URL`, `PROJECTX_USERNAME`, `PROJECTX_API_KEY`, `WEBHOOK_SECRET`, `TV_PORT`.
- **Accounts:** `ACCOUNT_<NAME>=<TOPSTEP_ACCOUNT_ID>` (e.g., `ACCOUNT_BETA=123456`); the first account becomes the default.
- **AI routing:** `N8N_AI_URL` plus optional per-account URLs (`N8N_AI_URL_ALPHA`, `N8N_AI_URL_BETA`, etc.).
- **Supabase:** `SUPABASE_URL`, `SUPABASE_KEY` for data + storage.
- **Trading config:** `OVERRIDE_CONTRACT_ID`, `STOP_LOSS_POINTS`, `TP_POINTS`, `TICKS_PER_POINT`, `MAX_DAILY_LOSS`, `DAILY_PROFIT_TARGET`, `MAX_CONSECUTIVE_LOSSES`.
- **Logging:** Optional `LOG_FILE` and `LOG_LEVEL` consumed by `logging_config.py`.

## 3. Runtime components

- **Flask webhook + scheduler** (`tradingview_projectx_bot.py`): exposes `/webhook`, starts the APScheduler 5m trigger, and hosts the dashboard/health endpoints.
- **SignalR listener** (`signalr_listener.py`): launched from the main entrypoint to capture broker events and write `trade_results`.
- **Position context** (`position_manager.py`): builds snapshots for the AI decision workflow.
- **Strategy execution** (`strategies.py`): currently supports the `simple` execution path invoked by the webhook handler.
- **Dashboard** (`dashboard.py` + `templates/dashboard.html`): merged feed visualization served by Flask.
- **Log sync utility** (`upload_botlog.py`): rotates `/tmp/tradingview_projectx_bot.log*` files and uploads them to the Supabase `botlogs` bucket.

## 4. System layout (recommended)

```
/opt/tradingview-bot/            # Git checkout or deploy artifact
/opt/tradingview-bot/.venv/      # Python virtualenv for the bot
/etc/tradingview-bot/.env        # Environment file loaded by systemd units
/var/log/tradingview-bot/        # (optional) symlink target for log file if LOG_FILE overrides /tmp
```

## 5. systemd services and timers

Place unit files in `/etc/systemd/system/` and reload with `sudo systemctl daemon-reload` after edits.

### 5.1 Core bot service

`/etc/systemd/system/tradingview-bot.service`

```
[Unit]
Description=TradingView ProjectX Bot (webhook + scheduler + SignalR)
After=network-online.target
Wants=network-online.target

[Service]
User=tradingbot
Group=tradingbot
WorkingDirectory=/opt/tradingview-bot
EnvironmentFile=/etc/tradingview-bot/.env
ExecStart=/opt/tradingview-bot/.venv/bin/python tradingview_projectx_bot.py
Restart=on-failure
RestartSec=5s
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 5.2 Log upload service + timer

`/etc/systemd/system/tradingview-bot-log.service`

```
[Unit]
Description=Upload rotated bot logs to Supabase storage
After=network-online.target

[Service]
Type=oneshot
User=tradingbot
Group=tradingbot
WorkingDirectory=/opt/tradingview-bot
EnvironmentFile=/etc/tradingview-bot/.env
ExecStart=/opt/tradingview-bot/.venv/bin/python upload_botlog.py
```

`/etc/systemd/system/tradingview-bot-log.timer`

```
[Unit]
Description=Nightly log sync for TradingView ProjectX Bot

[Timer]
OnCalendar=*-*-* 03:05:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable with:

```bash
sudo systemctl enable --now tradingview-bot.service
sudo systemctl enable --now tradingview-bot-log.timer
```

### 5.3 Optional cron alternative

If systemd timers are unavailable, add a cron entry (e.g., `/etc/cron.d/tradingview-bot-log`):

```
5 3 * * * tradingbot cd /opt/tradingview-bot && /opt/tradingview-bot/.venv/bin/python upload_botlog.py >> /var/log/tradingview-bot/logsync.log 2>&1
```

## 6. Deployment checklist

1. **Provision accounts:** Confirm TopstepX API + combine account, TradingView CME data feed, Supabase project/bucket, and chart-img access where used.
2. **Install system deps:** `python3.10`, `python3.10-venv`, `systemd`, `git`, `curl`; open outbound HTTPS and Flask port (`TV_PORT`).
3. **Clone + install:**
   ```bash
   git clone <repo> /opt/tradingview-bot
   cd /opt/tradingview-bot
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. **Configure secrets:** Copy `.env` to `/etc/tradingview-bot/.env` and fill required keys. Verify `ACCOUNT_*` IDs and AI endpoints.
5. **Validate locally:** `python tradingview_projectx_bot.py` then hit `http://localhost:<TV_PORT>/healthz`. Check `/tmp/tradingview_projectx_bot.log` for startup logs.
6. **Register services:** Install the systemd unit + timer above, `systemctl enable --now` them, and tail logs with `journalctl -u tradingview-bot.service -f`.
7. **TradingView alerts:** Ensure alerts post JSON with the secret, account, strategy (`simple`), signal (`BUY`/`SELL`/`HOLD`/`FLAT`), symbol (e.g., `CON.F.US.MES.H26`), and size.

## 7. Observability and recovery

- **Logging:** Rotating file at `/tmp/tradingview_projectx_bot.log` plus journald output. Adjust `LOG_FILE` to point at `/var/log/tradingview-bot/bot.log` if desired.
- **Health check:** `GET /healthz` returns `{status: "ok"}` and can be used for uptime probes.
- **Trade result gaps:** Investigate `/tmp/trade_results_missing.jsonl` or `/tmp/trade_results_fallback.jsonl` if SignalR events are missing.
- **Manual flatten:** Send a `FLAT` signal via webhook to close all positions on the contract.
- **Restart policies:** Systemd `Restart=on-failure` handles transient issues; use `systemctl restart tradingview-bot.service` after config changes.

## 8. Data model reference

Supabase tables expected by the dashboard and logger:

- `ai_trading_log`: AI decisions and metadata (account, symbol, signal, size, reasoning URLs).
- `trade_results`: Realized trade outcomes (entry/exit, pnl, raw_trades, trace/session IDs).
- `ai_trade_feed`: View or ETL output joining decisions + results for dashboard consumption.

## 9. Security and safety

- Store API keys and account IDs only in the `.env` owned by a non-root service user.
- Restrict inbound traffic to the webhook port and require the `WEBHOOK_SECRET` on every alert.
- Start in simulation accounts and configure conservative risk limits (`MAX_DAILY_LOSS`, `MAX_CONSECUTIVE_LOSSES`) before live trading.

