

# TradingView → TopstepX Order Execution Bot

A lightweight Flask service that listens for TradingView webhooks and executes bracketed futures orders on TopstepX (via ProjectX Gateway API).  
Designed for the Micro E-mini S&P 500 (MES) with a fixed CME contract and built-in “get-flat” rules.

---

## 📦 Features

- **Market Entry** on BUY/SELL alerts  
- **Bracket Orders**:  
  - Stop-loss: 10 pts  
  - Three take-profit legs at +2.5 / +5 / +10 pts  
- **Single‐contract logic**:  
  - If same‐side already open → skip  
  - If opposite side exists → flatten before new entry  
- **Auto-flatten** all orders & positions:  
  - On `FLAT` signal  
  - Every day **3:10 PM–5:00 PM CT** (Topstep get-flat window)  
- **Hard-coded MES** contract ID (`CON.F.US.MES.M25`)  
- **Configurable** via `.env`

---

## 🛠️ Prerequisites

- Python 3.8+  
- A TopstepX ProjectX Gateway API subscription (username + API key)  
- A server or VPS (Ubuntu/Debian) with HTTPS / domain pointing at it  
- `nginx` (or equivalent) to proxy and SSL-terminate

---

## 🚀 Installation

```bash
# 1. Clone repo
git clone https://github.com/youruser/tradingview-bot.git
cd tradingview-bot

# 2. Create & activate venv
python3 -m venv venv
source venv/bin/activate

# 3. Install deps
pip install -r requirements.txt

# 4. Copy & edit your .env
cp .env.example .env
nano .env
# …fill in your PROJECTX_BASE_URL, PROJECTX_USERNAME, PROJECTX_API_KEY, optionally TV_PORT…

# 5. Run locally for testing
python tradingview_projectx_bot.py
````

---

## 📄 `.env.example`

```ini
# .env.example
PROJECTX_BASE_URL=https://api.topstepx.com
PROJECTX_USERNAME=your_topstep_username
PROJECTX_API_KEY=sk-YOUR_API_KEY_HERE
# (Optional) to pick a specific account instead of the first:
# PROJECTX_ACCOUNT_ID=1234567

# The port your Flask app will listen on
TV_PORT=5000
```

---

## ⚙️ nginx (reverse-proxy + SSL)

```nginx
server {
    listen 80;
    server_name alerts.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name alerts.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/alerts.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/alerts.yourdomain.com/privkey.pem;

    location /webhook {
        proxy_pass http://127.0.0.1:5000/webhook;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

And obtain a cert with:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d alerts.yourdomain.com
```

---

## 📝 Usage

1. **TradingView Alert**

   * **Webhook URL**:

     ```
     https://alerts.yourdomain.com/webhook
     ```
   * **Message** (JSON):

     ```json
     {
       "symbol": "{{ticker}}",      // e.g. "MESM25" or "CON.F.US.MES.M25"
       "signal": "BUY",            // BUY, SELL or FLAT
       "size": 3                   // number of contracts
     }
     ```
2. **On BUY/SELL**

   * Skips if same‐side already open
   * Flattens opposite side if present
   * Places market entry for N contracts
   * Reads your fill price from recent trades
   * Posts one stop-loss + three TP limit orders
3. **On FLAT** (or between 3:10 PM–5:00 PM CT)

   * Cancels all open orders
   * Closes all positions

---

## 🛡️ Service Setup (systemd + Gunicorn)

1. **Install Gunicorn** in your venv:

   ```bash
   pip install gunicorn
   ```
2. **Create** `/etc/systemd/system/tvbot.service`:

   ```ini
   [Unit]
   Description=TradingView→TopstepX Bot
   After=network.target

   [Service]
   WorkingDirectory=/opt/tradingview-bot
   EnvironmentFile=/opt/tradingview-bot/.env
   ExecStart=/opt/tradingview-bot/venv/bin/gunicorn \
     --bind 0.0.0.0:5000 \
     tradingview_projectx_bot:app
   Restart=always
   User=root

   [Install]
   WantedBy=multi-user.target
   ```
3. **Enable & start**:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable tvbot.service
   sudo systemctl start tvbot.service
   sudo journalctl -u tvbot.service -f
   ```

---

## 📚 References

* **ProjectX Gateway API**:
  [https://gateway.docs.projectx.com/docs/intro](https://gateway.docs.projectx.com/docs/intro)
* **Topstep Trading Rules (“Get Flat”)**:
  [https://help.topstep.com/en/articles/8284206-when-and-what-products-can-i-trade](https://help.topstep.com/en/articles/8284206-when-and-what-products-can-i-trade)

---

<sup>MIT License</sup>

```


