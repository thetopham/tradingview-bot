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

- `ACCOUNT_beta=11065802` (example)
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

## Supabase schema

Two source tables exist:

- `ai_trading_log` – AI decisions (account, symbol, signal, size, reason, urls)
- `trade_results` – realized results on close (entry/exit/pnl, raw_trades, trace/session ids)

### Merged feed view (recommended)

Create a view that joins them by `ai_decision_id`:

```sql
create or replace view public.ai_trade_feed_v as
with tr_agg as (
  select
    ai_decision_id,
    min(entry_time) as entry_time,
    max(exit_time)  as exit_time,
    sum(total_pnl)  as total_pnl
  from public.trade_results
  where ai_decision_id is not null
  group by ai_decision_id
)
select
  ai.ai_decision_id,
  ai."timestamp" as decision_time,
  tr.entry_time,
  tr.exit_time,
  ai.account,
  ai.symbol,
  ai.signal,
  ai.size,
  tr.total_pnl,
  ai.reason,
  coalesce(ai.urls->>'5m', ai.urls->>'1m', ai.urls->>'15m', ai.urls->>'30m') as screenshot_url,
  ai.strategy
from public.ai_trading_log ai
left join tr_agg tr
  on tr.ai_decision_id = ai.ai_decision_id;
```

The dashboard will try `ai_trade_feed_v` first, and fall back to `ai_trade_feed` if you later materialize it.

## Notes

- Trade logging depends on SignalR close events. If you see missing results:
  - Check `/tmp/trade_results_missing.jsonl` and `/tmp/trade_results_fallback.jsonl`
  - Ensure the bot can query ProjectX trades (`/api/Trade/search`)
- The AI workflow should output **valid JSON only** to avoid parser issues in n8n.

## Safety

This code executes real orders. Use a sim account first and add risk controls before trading live.
