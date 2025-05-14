
# TradingView Alerts → TopstepX Order Bot

A lightweight Flask service that receives TradingView webhook alerts, executes bracketed orders through ProjectX api to TopstepX, and enforces Topstep “get-flat” rules and trading hours.

---

## ⚙️ Features

- **Market entry** on BUY/SELL alerts  
- **Stop-loss** (10 pts) and **3 take-profit legs** (2.5, 5, 10 pts) anchored to your *actual* fill price  
- **Automatic “flatten all”** of orders & positions at 3:10 PM CT (Mon–Fri)  
- **Trading-hours filter**: only processes alerts between 5:00 PM CT and 3:10 PM CT  
- **Manual FLAT** signal to cancel & close on demand  
- **Opposing-signal logic**: only cancels/closes when switching direction; otherwise stacks same-side entries  
- **Weighted fill-price** lookup via `/api/Trade/search`  

---

## 📋 Prerequisites

- Python 3.8+
- TopstepX account linked to ProjectX
- ProjectX Gateway API subscription (API key, username, account ID)  
- A server (e.g. VPS/Droplet) with HTTPS & domain/webhook configured  
- (Optional) Docker, systemd or another process manager  

---

## 🛠 Installation & Setup

1. **Clone the repo**  
   ```bash
   git clone https://github.com/thetopham/tradingview-bot.git
   cd tradingview-bot


2. **Create & activate a virtual environment**

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure environment variables**
   Copy `.env.example` → `.env`, then edit:

   ```ini
   PROJECTX_BASE_URL=https://gateway.docs.projectx.com
   PROJECTX_USERNAME=your_username
   PROJECTX_API_KEY=sk-…
   PROJECTX_ACCOUNT_ID=123456
   TV_PORT=5000
   ```

   *Optional:* adjust `STOP_LOSS_POINTS` and `TP_POINTS` inside the script.

4. **Run locally**

   ```bash
   source venv/bin/activate
   python tradingview_projectx_bot.py
   ```

   Test with:

   ```bash
   curl -X POST http://127.0.0.1:5000/webhook \
     -H "Content-Type: application/json" \
     -d '{"symbol":"MES1!","signal":"BUY","size":3}'
   ```

5. **Deploy to production**

   * Use Gunicorn + Nginx, Docker, or your preferred host.
   * Configure your webhook URL to `https://YOUR_DOMAIN/webhook`.
   * Secure with Let’s Encrypt or equivalent SSL.

---

## 🚀 Usage

### 1. TradingView Alert

Set up your alert with a JSON payload:

```json
{
  "symbol":   "MES1!",
  "signal":   "BUY",     // or "SELL" or "FLAT"
  "size":     3          // total contracts in bracket
}
```

### 2. Webhook Processing

* **BUY/SELL**

  1. Lookup active `contractId`
  2. If an *opposite* position exists → cancel all open orders & close it
  3. Place market entry for `size` contracts
  4. Fetch recent trades to compute a size-weighted fill price
  5. Place a stop-loss at *fillPrice ±10 pts*
  6. Split `size` evenly across three TP limit orders at *fillPrice ±{2.5,5,10} pts*

* **FLAT**
  Cancels **all** open orders and closes **all** open positions immediately.

### 3. Scheduled “Get Flat”

A background scheduler automatically runs **flatten\_all()** at **3:10 PM CT** Monday–Friday, ensuring compliance with Topstep’s daily close rules.

### 4. Trading-Hours Guard

Alerts arriving **between 3:10 PM CT and 5:00 PM CT** (off-hours) are acknowledged but **skipped**, preventing execution during the daily maintenance window. All other times (5:00 PM CT → 3:10 PM CT) are live trading hours.


---

## 🔧 Configuration

* **Environment variables** (in `.env`):

  * `PROJECTX_BASE_URL`
  * `PROJECTX_USERNAME`
  * `PROJECTX_API_KEY`
  * `PROJECTX_ACCOUNT_ID`
  * `TV_PORT`

* **Script parameters** (edit at top of `tradingview_projectx_bot.py`):

  * `STOP_LOSS_POINTS` (default: `10.0`)
  * `TP_POINTS` (default: `[2.5, 5.0, 10.0]`)
  * Trading window start/end (Central Time)

---

## 🔄 Customization

* **Bracket sizing**: adjust SL/TP point offsets
* **Trading window**: change `TRADING_START`/`TRADING_END` times
* **Scheduler**: modify APScheduler cron for different cutoffs
* **Logging & notifications**: integrate Slack, email, or Prometheus metrics

---

## 📚 References

* **ProjectX Gateway API**
  [https://gateway.docs.projectx.com/docs/intro](https://gateway.docs.projectx.com/docs/intro)
* **Topstep Trading Rules**
  [https://help.topstep.com/en/articles/8284206-when-and-what-products-can-i-trade](https://help.topstep.com/en/articles/8284206-when-and-what-products-can-i-trade)

---

## ⚖️ License

MIT License — see [LICENSE](./LICENSE) for details.



