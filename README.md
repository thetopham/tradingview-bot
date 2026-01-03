# TradingView ProjectX Bot (AI Day Trader Overseer)

This repo runs a lightweight execution + observability layer for an “AI day trader” workflow:

- **TradingView / scheduler webhook** hits the bot (`/webhook`)
- Bot gathers **position + risk context** and calls an **AI decision endpoint** (n8n workflow using vision LLMs)
- Bot **executes** the resulting signal (currently `simple` market entries)
- A **SignalR listener** watches broker events and logs **trade_results** on close
- A **dashboard** displays decisions + outcomes, and the merged feed can be used to **train a future model**

> Primary goal: produce a clean dataset that captures the full loop  
> **(context → hypothesis/reasoning → action → result/PnL)**.

## Components

- `tradingview_projectx_bot.py` – Flask app, webhook handler, AI routing, strategy dispatch
- `position_manager.py` – builds position + account context for the AI (no autonomous actions)
- `strategies.py` – execution strategies (currently `simple`)
- `signalr_listener.py` – listens to broker events; logs results when a position closes
- `api.py` – ProjectX REST calls + Supabase logging helpers
- `dashboard.py` + `dashboard.html` – UI and API endpoint for merged feed

## Local setup

### 1) Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2) Environment variables

Create a `.env` file (or set env vars in your runtime):

Required:

- `TV_PORT` – Flask port
- `WEBHOOK_SECRET` – shared secret for `/webhook`
- `SUPABASE_URL`, `SUPABASE_KEY`
- ProjectX / TopstepX credentials expected by `auth.py`

Accounts:

- `ACCOUNT_beta=topstep-account-number-goes-here` (example)
- `ACCOUNT_epsilon=...`

AI endpoint:

- `N8N_AI_URL=https://.../webhook/simple` (example)

Optional:

- `OVERRIDE_CONTRACT_ID=CON.F.US.MES.H26` (forces MES contract; see `api.get_contract`)

### 3) Run

```bash
python tradingview_projectx_bot.py
```

Then open:

- Dashboard: `http://localhost:<TV_PORT>/dashboard`

For production deployments (subscriptions, environment variables, systemd units, timers, and cron alternatives), see the detailed [Project Setup and Operations Guide](docs/PROJECT_SETUP.md).

## Supabase schema

Three source tables exist:

- `ai_trading_log` – AI decisions (account, symbol, signal, size, reason, urls)
- `trade_results` – realized results on close (entry/exit/pnl, raw_trades, trace/session ids)
- 'ai_trade_feed' - pulls data from ai_trading_log with ai hypothesis and entry data and pnl results from trade_results then displays on dashboard



## Notes

- Trade logging depends on SignalR close events. If you see missing results:
  - Check `/tmp/trade_results_missing.jsonl` and `/tmp/trade_results_fallback.jsonl`
  - Ensure the bot can query ProjectX trades (`/api/Trade/search`)
- The AI workflow should output **valid JSON only** to avoid parser issues in n8n.

### Trading hours (Mountain Time)

- Daily flatten window: **2:05pm–4:00pm MT (Mon–Fri)**
- Markets are closed/flat all day **Saturday**
- **Sunday reopen: 3:00pm MT**

## Safety

This code executes real orders. Use a sim account first and add risk controls before trading live.
