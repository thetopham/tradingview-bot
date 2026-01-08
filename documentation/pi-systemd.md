## Pi: systemd services + timers

This project runs as a **systemd service** on the Raspberry Pi and uses **systemd timers** for:
1) a scheduled daily restart of the bot service  
2) an hourly log upload/prune job to Supabase  

All units live in: `/etc/systemd/system/`

---

### `tradingview_bot.service` (main bot)

**File:** `/etc/systemd/system/tradingview_bot.service`

**Purpose:** Runs the TradingView AI Overseer bot as a long-running service.

**Key settings:**
- Runs as user/group: `thetopham`
- Working directory: `/home/thetopham/tradingview-bot`
- Loads env vars from: `/home/thetopham/tradingview-bot/.env`
- Forces log settings:
  - `LOG_FILE=/tmp/tradingview_projectx_bot.log`
  - `LOG_LEVEL=INFO`
- Starts the app:
  - `/home/thetopham/tradingview-bot/venv/bin/python3 /home/thetopham/tradingview-bot/tradingview_projectx_bot.py`
- Restart policy:
  - `Restart=always`
  - `RestartSec=5s`
- Output:
  - `StandardOutput=journal` (so logs also appear in `journalctl`)
  - file logging still handled by the app via `LOG_FILE`

**Unit content:**
```ini
[Unit]
Description=TradingView AI Overseer Bot
After=network.target

[Service]
User=thetopham
Group=thetopham
WorkingDirectory=/home/thetopham/tradingview-bot
EnvironmentFile=/home/thetopham/tradingview-bot/.env
Environment=LOG_FILE=/tmp/tradingview_projectx_bot.log
Environment=LOG_LEVEL=INFO
ExecStart=/home/thetopham/tradingview-bot/venv/bin/python3 /home/thetopham/tradingview-bot/tradingview_projectx_bot.py
Restart=always
RestartSec=5s
StartLimitIntervalSec=0
Type=simple
StandardOutput=journal
StandardError=inherit

[Install]
WantedBy=multi-user.target
```

---

### `tradingview_bot-restart.timer` + `tradingview_bot-restart.service` (scheduled restart)

**Files:**
- `/etc/systemd/system/tradingview_bot-restart.timer`
- `/etc/systemd/system/tradingview_bot-restart.service`

**Purpose:** Restarts `tradingview_bot.service` on a schedule (health/hygiene).

**Schedule:**
- Mon–Fri at **15:55:00 America/Denver**
- Sun at **15:55:00 America/Denver**
- `Persistent=true` (if the Pi was off, it runs the missed schedule on boot)
- `AccuracySec=1s`

**Timer content:**
```ini
[Unit]
Description=Restart tradingview_bot.service at 15:55 (America/Denver) on trading days

[Timer]
OnCalendar=Mon..Fri *-*-* 15:55:00
OnCalendar=Sun *-*-* 15:55:00
Persistent=true
AccuracySec=1s

[Install]
WantedBy=timers.target
```

**Service content:**
```ini
[Unit]
Description=Scheduled restart of tradingview_bot.service

[Service]
Type=oneshot
ExecStart=/bin/systemctl restart tradingview_bot.service
```

---

### `tradingview_botlog.timer` + `tradingview_botlog.service` (hourly log upload + prune)

**Files:**
- `/etc/systemd/system/tradingview_botlog.timer`
- `/etc/systemd/system/tradingview_botlog.service`

**Purpose:** Uploads & prunes bot logs to Supabase (runs `upload_botlog.py`).

**Schedule:**
- `OnCalendar=hourly`
- `Persistent=true`

**Timer content:**
```ini
[Unit]
Description=Hourly uploader for trading bot logs

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

**Service content:**
```ini
[Unit]
Description=Upload & prune trading bot logs (Supabase)

[Service]
Type=oneshot
User=thetopham
WorkingDirectory=/home/thetopham/tradingview-bot
EnvironmentFile=/home/thetopham/tradingview-bot/.env
ExecStart=/home/thetopham/tradingview-bot/venv/bin/python /home/thetopham/tradingview-bot/upload_botlog.py
```

---

## Ops commands (runbook)

### Service control
```bash
sudo systemctl daemon-reload
sudo systemctl enable tradingview_bot.service
sudo systemctl start tradingview_bot.service
sudo systemctl restart tradingview_bot.service
sudo systemctl status tradingview_bot.service
```

##after github updates
```bash
git pull
sudo systemctl daemon-reload
sudo systemctl restart tradingview_bot.service
```

### Logs (systemd journal)
```bash
sudo journalctl -u tradingview_bot.service -f
sudo journalctl -u tradingview_botlog.service -n 200 --no-pager
sudo journalctl -u tradingview_bot-restart.service -n 200 --no-pager
```

### Logs (file)
```bash
ls -lah /tmp/tradingview_projectx_bot.log*
tail -n 200 /tmp/tradingview_projectx_bot.log
```

### Timers
```bash
systemctl list-timers --all | grep -i trading
systemctl status tradingview_botlog.timer
systemctl status tradingview_bot-restart.timer
```

### Common troubleshooting
```bash
systemctl --failed --type=service
sudo journalctl -u tradingview_bot.service -n 300 --no-pager
systemctl cat tradingview_bot.service
```
