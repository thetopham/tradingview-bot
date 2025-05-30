

# TradingView Webhook Bot for ProjectX

This project implements a Python-based webhook designed to receive trading alerts from TradingView and execute trades on the ProjectX trading platform. It features automated trading strategies, integration with an AI decision-making service (via n8n), and comprehensive logging of trade results to Supabase. The bot is configurable via environment variables and includes utilities for log management.

---

## 📦 Core Features

- **Webhook Integration**: Receives TradingView alerts via HTTP POST requests.
- **Flexible Trading Strategies**: Supports multiple trading strategies like `bracket`, `brackmod`, and AI-assisted pivots.
- **ProjectX API**: Interacts with ProjectX for order execution and position management.
- **AI-Powered Decisions**: Optionally integrates with an n8n workflow to leverage AI (e.g., OpenAI) for trade decisions.
- **Supabase Logging**: Logs trade results and bot activity to a Supabase database.
- **Configuration**: Highly configurable via environment variables (`.env` file).
- **Log Management**: Includes a utility script (`upload_botlog.py`) for sending logs to Discord.
- **Automated Position Flattening**: Can flatten positions based on `FLAT` signals or pre-set times.
- **Phantom Order Sweeper**: Includes a mechanism to detect and cancel orphaned orders.

---

## Core Functionality

The bot operates as a webhook service, processing signals from TradingView to execute trades on ProjectX. Key functionalities include:

- **Webhook Setup**: Listens on the `/webhook` endpoint for incoming JSON payloads from TradingView, triggering trade actions based on the received signal (BUY, SELL, FLAT).
- **ProjectX API Interaction**:
    - **Authentication**: Securely connects to the ProjectX API using credentials stored in the `.env` file.
    - **Order Placement**: Places market orders and bracket orders (stop-loss and take-profit) based on the trading strategy.
    - **Position Management**: Monitors and manages open positions, including flattening positions when required (e.g., on a FLAT signal or during specific market hours).
- **Trading Strategies**:
    - **Bracket**: Standard bracket order with predefined stop-loss and take-profit levels.
    - **Brackmod**: A modified bracket strategy allowing dynamic adjustments based on market conditions or AI input.
    - **Pivot**: Trades based on pivot points, often integrated with AI analysis for entry and exit signals.
- **AI-Driven Decision-Making**:
    - Integrates with an n8n workflow that calls an AI service (e.g., OpenAI).
    - The AI analyzes market data and provides trade recommendations (e.g., hold, modify, or close position), which the bot can act upon.
- **Trade Result Logging**:
    - Records detailed information about each trade (entry price, exit price, profit/loss, strategy used) to a Supabase database for analysis and record-keeping.
- **`upload_botlog.py` Script**:
    - A utility script to manually or automatically upload local log files (e.g., `bot.log`) to a designated channel or storage (like a Discord channel) for monitoring and debugging.
- **Phantom Order Sweeper**:
    - A scheduled task that periodically checks for and cancels any "phantom" orders on ProjectX. These are orders that might have been orphaned due to disconnections or errors, ensuring the account state remains clean.

---

## 🛠️ Prerequisites

- Python 3.8+
- Access to a ProjectX compatible trading environment (e.g., a prop firm account like TopstepX, or direct ProjectX API access).
- ProjectX API Credentials (username, API key).
- A server or VPS (Ubuntu/Debian recommended) with a static IP address and a domain name pointing to it for HTTPS webhook.
- `nginx` (or a similar reverse proxy) for SSL termination and proxying requests to the bot.
- Supabase account for trade logging (optional but recommended).
- Discord webhook URL for log uploads (optional).
- n8n workflow URL for AI integration (optional).

---

## ⚙️ Setup and Configuration

Setting up the TradingView Webhook Bot involves configuring your environment and installing necessary dependencies.

### Environment Setup

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/yourusername/tradingview-webhook-bot.git # Replace with your repository URL
    cd tradingview-webhook-bot
    ```
2.  **Create a Virtual Environment**:
    It's highly recommended to use a virtual environment to manage project dependencies.
    ```bash
    python3 -m venv venv
    source venv/bin/activate  # On Windows use `venv\Scripts\activate`
    ```
3.  **Install Dependencies**:
    Install all required Python packages using the `requirements.txt` file:
    ```bash
    pip install -r requirements.txt
    ```

### Environment Variables

The bot is configured using environment variables. Create a `.env` file in the root of the project directory by copying the `env.example` file:

```bash
cp env.example .env
```

Then, edit the `.env` file with your specific settings.

**Required Environment Variables:**

*   `PROJECTX_BASE_URL`: The base URL for the ProjectX API (e.g., `https://api.topstepx.com`).
*   `PROJECTX_USERNAME`: Your username for the ProjectX API.
*   `PROJECTX_API_KEY`: Your API key for the ProjectX API.
*   `WEBHOOK_SECRET`: A secret key to verify webhook requests. This should be a long, random string that you also use in your TradingView alert configuration.
*   `TV_PORT`: The local port on which the Flask webhook listener will run (e.g., `5000`).
*   `N8N_WEBHOOK_URL` (Optional): URL for your n8n workflow if using AI integration for strategies like `pivot_ai`.
*   `SUPABASE_URL` (Optional): The URL of your Supabase project for logging trade results.
*   `SUPABASE_KEY` (Optional): The anon key for your Supabase project.
*   `DISCORD_WEBHOOK_URL` (Optional): The Discord webhook URL for uploading bot logs.
*   `ACCOUNT_<NAME>`: Defines a friendly name for a ProjectX account ID. You can have multiple such variables (e.g., `ACCOUNT_MAIN=12345`, `ACCOUNT_TEST=67890`). The bot will use the first account found if `PROJECTX_ACCOUNT_ID` is not explicitly set.
*   `PROJECTX_ACCOUNT_ID` (Optional): If you want to specify a default ProjectX account ID to use. If not set, the bot may use the first account defined with `ACCOUNT_<NAME>` or the first account returned by the API.

**Refer to `env.example` for a template. Your `.env` file should look something like this:**
```ini
# ProjectX API Credentials
PROJECTX_BASE_URL=https://api.projectx.com
PROJECTX_USERNAME=your_px_username
PROJECTX_API_KEY=sk_your_px_api_key

# Webhook Configuration
WEBHOOK_SECRET=a_very_strong_and_unique_secret_key
TV_PORT=5000

# n8n Integration (Optional)
N8N_WEBHOOK_URL=https://your.n8n.instance/webhook/tradingview-ai

# Supabase Logging (Optional)
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_supabase_anon_key

# Discord Log Upload (Optional)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your_discord_webhook_id/your_discord_webhook_token

# Account Mapping (Friendly Name -> ProjectX Account ID)
# Example: ACCOUNT_EVAL=123456
# Example: ACCOUNT_FUNDED=789012
ACCOUNT_MYACCOUNT=100001
PROJECTX_ACCOUNT_ID=100001 # Optional: Specify default account ID to use if 'account' field is missing in webhook
```

---

## ▶️ Running the Bot

Once the setup and configuration are complete, you can run the bot.

### Main Bot Application (`tradingview_projectx_bot.py`)

The main application is a Flask-based webhook listener.

**Development:**

For development and testing, you can run the Flask development server directly:

```bash
# Ensure your virtual environment is activated
source venv/bin/activate 

# Run the bot
python tradingview_projectx_bot.py
```
The bot will start listening on the port specified by the `TV_PORT` environment variable (default is `5000`).

**Production:**

For a production environment, it's recommended to use a more robust WSGI server like Gunicorn. See the `🛡️ Deployment with Gunicorn and systemd` and `🌐 Nginx Setup for Reverse Proxy and SSL` sections for details.

---

## 📡 Webhook Data Format

The bot expects a JSON payload on the `/webhook` endpoint. This payload contains the necessary information to execute a trade or manage positions.

**Endpoint:** `/webhook`
**Method:** `POST`
**Content-Type:** `application/json`

### Payload Fields:

*   **`secret`** (string, required):
    *   A security token used to authenticate the webhook request.
    *   **Important**: This value *must* match the `WEBHOOK_SECRET` environment variable configured for the bot. Requests with an invalid secret will be rejected.
*   **`strategy`** (string, required):
    *   The name of the trading strategy to be executed.
    *   Examples: `bracket`, `brackmod`, `pivot_ai`.
    *   The bot uses this field to determine the order placement logic (e.g., bracket parameters, AI interaction).
*   **`account`** (string, optional):
    *   The friendly name of the ProjectX account to use for the trade (e.g., `EVAL`, `FUNDED`).
    *   This name should correspond to one of the `ACCOUNT_<NAME>` environment variables (e.g., `ACCOUNT_EVAL=123456`).
    *   If not provided, the bot will use the account specified by `PROJECTX_ACCOUNT_ID` or the default account logic.
*   **`signal`** (string, required):
    *   The trading signal.
    *   Valid values:
        *   `BUY`: Indicates a long entry.
        *   `SELL`: Indicates a short entry.
        *   `FLAT`: Indicates to close all open positions and cancel all working orders for the specified symbol and account.
*   **`symbol`** (string, required):
    *   The trading symbol for the instrument.
    *   This should match the symbol format expected by ProjectX (e.g., `MESU4`, `CON.F.US.MES.U24`).
*   **`size`** (integer, optional):
    *   The number of contracts to trade.
    *   Defaults to `1` if not provided.
    *   For `FLAT` signals, this field is typically ignored.
*   **`alert`** (object, optional):
    *   An object containing additional data from the TradingView alert, which might be used by specific strategies or for logging.
    *   Common fields within `alert` could include `plot_name`, `price_action`, `indicator_value`, etc. The structure is flexible.

### Example JSON Payload:

```json
{
  "secret": "your_webhook_secret_here",
  "strategy": "brackmod",
  "account": "EVAL",
  "signal": "BUY",
  "symbol": "MESU4",
  "size": 2,
  "alert": {
    "plot_name": "Long Signal",
    "price_action": "Breakout",
    "timestamp": "2023-10-26T10:30:00Z"
  }
}
```

### Log Upload Utility (`upload_botlog.py`)

The `upload_botlog.py` script is used to upload the `bot.log` file (and its rotated versions) to a configured Discord channel. This is useful for monitoring the bot's activity and for debugging.

**Manual Execution:**

```bash
# Ensure your virtual environment is activated
source venv/bin/activate

# Run the script
python upload_botlog.py
```
The script will look for `bot.log` (and `bot.log.1`, `bot.log.2`, etc.) in the current directory and upload them. Ensure `DISCORD_WEBHOOK_URL` is set in your `.env` file.

**Automated Execution (Cron Job):**

You can automate the log upload process using a cron job. For example, to run the script every hour:

1.  Open your crontab editor: `crontab -e`
2.  Add a line similar to this, adjusting the paths as necessary:

    ```cron
    0 * * * * /path/to/your/project/venv/bin/python /path/to/your/project/upload_botlog.py >> /path/to/your/project/cron.log 2>&1
    ```

    This example runs the script at the start of every hour. The output and any errors from the cron job will be appended to `cron.log`. Ensure the script has execute permissions (`chmod +x upload_botlog.py`) if you run it directly without `python`.

---

## 📄 Example `env.example` File Content

Your `env.example` file should be updated to reflect all configurable variables:

```ini
# ProjectX API Credentials
PROJECTX_BASE_URL=https://api.projectx.com
PROJECTX_USERNAME=
PROJECTX_API_KEY=

# Webhook Configuration
WEBHOOK_SECRET=
TV_PORT=5000

# n8n Integration (Optional)
N8N_WEBHOOK_URL=

# Supabase Logging (Optional)
SUPABASE_URL=
SUPABASE_KEY=

# Discord Log Upload (Optional)
DISCORD_WEBHOOK_URL=

# Account Mapping (Friendly Name -> ProjectX Account ID)
# Example: ACCOUNT_EVAL=123456
# Example: ACCOUNT_FUNDED=789012
ACCOUNT_MYACCOUNT=
PROJECTX_ACCOUNT_ID= # Optional: Specify default account ID
```

*Note: The `🚀 Installation` section, which was largely redundant with `⚙️ Setup and Configuration`, has been removed. The content related to Gunicorn and Nginx has been moved to dedicated deployment sections.*

---

## 🌐 Nginx Setup for Reverse Proxy and SSL
## 🌐 Nginx Setup for Reverse Proxy and SSL

For production, you should run the Flask app behind Nginx, which will handle HTTPS and proxy requests to Gunicorn.

**Example Nginx Configuration:**

Save this configuration in `/etc/nginx/sites-available/tradingview_bot`:

```nginx
server {
    listen 80;
    server_name your_domain.com; # Replace with your domain
    # Redirect HTTP to HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your_domain.com; # Replace with your domain

    # SSL Configuration (using Let's Encrypt)
    ssl_certificate /etc/letsencrypt/live/your_domain.com/fullchain.pem; # Adjust path
    ssl_certificate_key /etc/letsencrypt/live/your_domain.com/privkey.pem; # Adjust path
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # Logging
    access_log /var/log/nginx/tradingview_bot.access.log;
    error_log /var/log/nginx/tradingview_bot.error.log;

    location / {
        # Deny access to other paths if your bot is only at /webhook
        return 403;
    }

    location /webhook {
        proxy_pass http://127.0.0.1:5000; # Assumes Gunicorn runs on port 5000
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Enable the site and obtain SSL certificate with Certbot:**

```bash
sudo ln -s /etc/nginx/sites-available/tradingview_bot /etc/nginx/sites-enabled/
sudo nginx -t # Test configuration
sudo systemctl reload nginx

# Install Certbot and get certificate
sudo apt update
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d your_domain.com # Replace with your domain
```
Ensure your firewall allows traffic on ports 80 and 443.

---

## 📝 TradingView Alert Setup

To send alerts from TradingView to your bot:

1.  **Webhook URL**:
    In TradingView alert settings, use the URL `https://your_domain.com/webhook` (replace `your_domain.com` with your actual domain).

2.  **Message Body (JSON)**:
    Use the JSON format specified in the `📡 Webhook Data Format` section. Ensure the `secret` field matches your `WEBHOOK_SECRET` environment variable.

    **Example Minimal Message:**
    ```json
    {
      "secret": "your_webhook_secret_here",
      "strategy": "bracket",
      "signal": "BUY",
      "symbol": "MESU4",
      "size": 1
    }
    ```

    **Example with Optional Fields:**
    ```json
    {
      "secret": "your_webhook_secret_here",
      "strategy": "pivot_ai",
      "account": "EVAL",
      "signal": "SELL",
      "symbol": "MNQU4",
      "size": 1,
      "alert": {
        "indicator": "SuperTrend",
        "price": 18000.50
      }
    }
    ```

---

## 🛡️ Deployment with Gunicorn and systemd

1.  **Install Gunicorn** in your virtual environment:
    ```bash
    # Ensure your virtual environment is activated
    # source /path/to/your/project/venv/bin/activate 
    pip install gunicorn
    ```

2.  **Create systemd Service File**:
    Create a file named `tradingview_bot.service` in `/etc/systemd/system/`:
    ```ini
    [Unit]
    Description=TradingView Webhook Bot for ProjectX
    After=network.target

    [Service]
    User=your_username       # Replace with the user that owns the bot files (not root preferably)
    Group=your_groupname     # Replace with the group for the user
    WorkingDirectory=/path/to/your/tradingview-webhook-bot # Replace with the actual path
    EnvironmentFile=/path/to/your/tradingview-webhook-bot/.env # Path to your .env file
    ExecStart=/path/to/your/tradingview-webhook-bot/venv/bin/gunicorn \
        --workers 3 \
        --bind unix:/path/to/your/tradingview-webhook-bot/tradingview_bot.sock \
        tradingview_projectx_bot:app # Assuming your main Python file is tradingview_projectx_bot.py and Flask app instance is 'app'
    
    Restart=always
    RestartSec=5s

    [Install]
    WantedBy=multi-user.target
    ```
    *   **Important**:
        *   Replace `your_username`, `your_groupname`, and `/path/to/your/tradingview-webhook-bot` with appropriate values.
        *   Using a Unix socket (`unix:/path/to/your/tradingview-webhook-bot/tradingview_bot.sock`) is generally recommended for Gunicorn when Nginx is on the same server. Adjust your Nginx `proxy_pass` directive accordingly (e.g., `proxy_pass http://unix:/path/to/your/tradingview-webhook-bot/tradingview_bot.sock;`). If you prefer to use a TCP port (e.g., `127.0.0.1:5000`), change `bind` in Gunicorn and `proxy_pass` in Nginx.
        *   Ensure the `WorkingDirectory` and paths in `ExecStart` are correct.

3.  **Reload systemd, Enable and Start the Service**:

   ```bash
    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable tradingview_bot.service
    sudo systemctl start tradingview_bot.service
    sudo systemctl status tradingview_bot.service
    # To see logs:
    # sudo journalctl -u tradingview_bot.service -f
    ```

---

## 📚 Additional Notes and References

*   **ProjectX API Documentation**: For detailed information on ProjectX API endpoints, authentication, and capabilities, refer to the official ProjectX Gateway API documentation (e.g., `https://gateway.docs.projectx.com/docs/intro` - always check for the most current link from your provider).
*   **Security**:
    *   Keep your `WEBHOOK_SECRET` and API keys confidential.
    *   Ensure your server is secured and updated regularly.
    *   Restrict access to the webhook endpoint if possible (e.g., firewall rules for TradingView IP addresses, though these can change).
*   **Error Handling**: The bot includes basic error handling, but you may want to enhance it based on your needs. Check `bot.log` for errors.
*   **Customization**: Feel free to customize the strategies, logging, and notification mechanisms.

---

## ⚠️ Disclaimer

**Trading financial markets involves substantial risk of loss and is not suitable for every investor. The value of financial instruments can and frequently does fluctuate, and as a result, clients may lose more than their original investment.**

This software, "TradingView Webhook Bot for ProjectX," is provided for informational, educational, and experimental purposes only. It is not intended as, and should not be considered, financial advice, investment advice, or a recommendation to buy or sell any financial instrument.

The strategies and configurations implemented within this bot are examples and may not be suitable for your specific trading objectives, financial situation, or risk tolerance. Past performance is not indicative of future results.

**Users of this software are solely responsible for their trading decisions and any resulting outcomes. You should test this software thoroughly in a simulated or paper trading environment before deploying it with real funds. Use this software at your own risk.**

The developers and contributors of this software assume no liability for any financial losses or damages incurred through the use of this software.

---

<sup>MIT License</sup>




