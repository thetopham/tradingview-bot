# TradingView ProjectX Bot – Full System Documentation

> **Purpose**: This document explains **everything required to recreate the TradingView ProjectX AI trading system** from scratch — infrastructure, accounts, data flows, AI logic, scheduling, storage, risk rules, and dashboards.

This is written as a **build-from-zero reference**, not marketing copy.

---

## 1. System Overview

This project is a **research-grade AI day trading system** designed to:

- Observe markets via **TradingView charts + numeric data**
- Produce **AI trading decisions** (hypothesis → action)
- Execute trades via **TopstepX** (server-side)
- Log **full trade lifecycle** (context → decision → execution → PnL)
- Display results in a **live dashboard**
- Generate a **clean dataset** for future model training

Core principle:

> **Context → Reasoning → Action → Result**

---

## 2. Accounts & Market Access

### 2.1 Trading Account – TopstepX Combine

- **Account Type**: TopstepX Combine
- **Account Size**: \$50,000

#### Risk Rules (Critical)

- Account fails below **\$48,000**
- Max loss: **\$2,000**
- Above \$50k:
  - \$2,000 **trailing drawdown** from equity peak

#### Instrument

- MES (Micro E-mini S&P)
- \~\$5 per point per contract

#### Trading Hours (Mountain Time)

- Daily flatten: **2:05pm – 4:00pm MT**
- Saturday: flat all day
- Sunday reopen: **4:00pm MT**

These rules are **explicitly injected into AI context**.

---

## 3. Subscriptions & Costs

| Service               | Purpose                | Approx Cost           |
| --------------------- | ---------------------- | --------------------- |
| TradingView Essential | Chart layouts + alerts | \~\$9.69/mo (BF deal) |
| CME Real-Time Data    | MES data               | \~\$7/mo              |
| Chart-Img API         | TradingView → JPG      | \~\$7–10/mo           |
| Supabase              | Database + storage     | Free tier             |
| n8n                   | Workflow orchestration | Free (self-hosted)    |
| Raspberry Pi          | Bot host               | One-time              |

---

## 4. Infrastructure

### 4.1 Hardware

- **Raspberry Pi** (always-on)
- Runs:
  - Flask server
  - APScheduler
  - SignalR listener

### 4.2 Hosting

- Self-hosted services
- Optional domain (e.g. `n8n.yourdomain.com`)

---

## 5. Core Components

### 5.1 Flask Bot (Execution Layer)

**Main file**: `tradingview_projectx_bot.py`

Responsibilities:

- Receives webhook triggers
- Validates secrets
- Gathers position context
- Calls AI (n8n)
- Executes trades
- Routes results to logging

---

### 5.2 n8n (AI Orchestration)

Used for:

- AI reasoning workflows
- Chart image ingestion
- Data feed processing

Workflows:

- **Overseer AI** (5m / 15m / 30m)
- **Chart Fetch** (5m / 15m / 30m)
- **Datafeed ingestion**

n8n is **logic-only** — no execution authority.

---

### 5.3 Chart Image Pipeline

1. TradingView layout is configured manually
2. Chart-Img API fetches:
   - 1920x1080 JPG
3. Image stored in:
   - Google Cloud Storage bucket
4. URL saved to Supabase
5. Passed into AI vision model

---

## 6. Chart Layouts (AI Input)

### Timeframes

- 5m
- 15m
- 30m

### Indicators

- EMA 9
- EMA 21
- VWAP (session)
- MACD (12, 26, 9)
- ATR

These are **hard-coded expectations** in the AI prompt.

---

## 7. Data Feeds - supabase

### Supabase Tables

#### Raw Market Data

- `tv_datafeed`
- `tv_datafeed_5m`
- `tv_datafeed_15m`
- `tv_datafeed_30m`

Contains:

- OHLC
- Volume
- Indicators

---

## 8. AI Decision Logging - supabase

### ai\_trading\_log

Stores:

- AI decision ID
- Account
- Symbol
- Signal
- Size
- Reason (natural language)
- Prompt version
- Screenshot URL

---

## 9. Trade Results Logging - supabase

### trade\_results

Logged **only when position closes**:

- Entry time
- Exit time
- Entry price
- Exit price
- Gross PnL
- Fees
- Net PnL
- Raw trades
- Trace ID
- Session ID

Includes idempotency logic to prevent duplicates.

---

## 10. Unified Dataset -supabase 

### ai\_trade\_feed

A merged dataset combining:

- AI hypothesis
- Execution data
- Final PnL

This table powers:

- Dashboard
- Model training
- Performance analysis

---

## 11. Position & Risk Context (AI Input)

Provided every AI call:

### Current Position

- Has position
- Side
- Size
- Entry price
- Current price
- Unrealized PnL
- Duration

### Account Metrics

- Daily PnL
- Win rate
- Consecutive losses
- Account balance

### Topstep Risk

- Equity peak
- Trailing DD used
- Trailing DD remaining
- Risk state: green / yellow / red

### Warnings

- Near daily loss
- Loss streak
- Trailing DD danger

---

## 12. AI Rules (Hard Constraints)

- Signals allowed: BUY / SELL / HOLD / FLAT
- Must obey Topstep rules
- If risk state is red → HOLD/FLAT preferred
- After loss → momentum improvement required
- HOLD preferred over forced trades

AI **does not manage stops** — broker does.

---

## 13. Scheduler (APScheduler)

### Chart Prefetch

- 5m: every 5 minutes
- 15m: every 15 minutes
- 30m: every 30 minutes

### Overseer Calls

- Triggered \~15s after bar close

### Auto Flatten

- Enforced at 2:05pm MT

---

## 14. Dashboard

### Features

- Live PnL
- Win rate
- Open positions
- Trade history
- Screenshot viewer

### URL

```
http://localhost:<PORT>/dashboard
```

---

## 15. Logging & Observability

- Rotating local logs
- Uploads to Supabase Storage
- SignalR event tracking
- Trade reconstruction on restart

---

## 16. Environment Variables

Required:

- `ACCOUNT_<NAME>=<TOPSTEP_ID>`
- `PROJECTX_USERNAME`
- `PROJECTX_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_KEY`
- `WEBHOOK_SECRET`
- `N8N_AI_URL`

Optional:

- `N8N_5MCHART_FETCH_URL`
- `N8N_15MCHART_FETCH_URL`
- `N8N_30MCHART_FETCH_URL`

---

## 17. Safety Disclaimer

⚠️ **This system places real trades**.

- Use simulation first
- Enforce risk limits
- Monitor continuously

---

## 18. Final Notes

This system is intentionally designed as:

> **A research agent that trades**

Not a black-box profit machine.

Its real value is the **dataset it produces**.

---

*End of documentation*

