# tradingview_projectx_bot.py

import os
import time
import threading
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# ─── Config ─────────────────────────────────────────────
TV_PORT       = int(os.getenv("TV_PORT", 5000))
PX_BASE       = os.getenv("PROJECTX_BASE_URL", "https://gateway-api-demo.s2f.projectx.com")
USER_NAME     = os.getenv("PROJECTX_USERNAME")
API_KEY       = os.getenv("PROJECTX_API_KEY")
ACCOUNT_ID    = int(os.getenv("PROJECTX_ACCOUNT_ID"))

# ─── Globals ────────────────────────────────────────────
app = Flask(__name__)
token = None
token_expiry = 0
lock = threading.Lock()

# ─── Helpers ────────────────────────────────────────────
def authenticate():
    """
    Step 1: POST /api/Auth/loginKey to get a session token.
    :contentReference[oaicite:0]{index=0}
    """
    global token, token_expiry
    resp = requests.post(
        f"{PX_BASE}/api/Auth/loginKey",
        json={"userName": USER_NAME, "apiKey": API_KEY},
        headers={"Content-Type": "application/json"}
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError("Auth failed: " + str(data))
    token = data["token"]
    token_expiry = time.time() + 23*3600  # refresh a bit before 24h

def ensure_token():
    with lock:
        if token is None or time.time() >= token_expiry:
            authenticate()

def search_contract(symbol: str) -> str:
    """
    Step 2: POST /api/Contract/search to find contractId for `symbol`
    :contentReference[oaicite:1]{index=1}
    """
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Contract/search",
        json={"searchText": symbol, "live": True},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    data = resp.json()
    for c in data.get("contracts", []):
        if c.get("activeContract"):
            return c["id"]
    raise ValueError(f"No active contract found for {symbol}")

def place_order(contract_id: str, side: str, size: int):
    """
    Step 3: POST /api/Order/place to send market BUY/SELL
    :contentReference[oaicite:2]{index=2}
    """
    ensure_token()
    side_int = 0 if side.upper() == "BUY" else 1
    payload = {
        "accountId": ACCOUNT_ID,
        "contractId": contract_id,
        "type": 2,          # Market
        "side": side_int,   # 0=Buy,1=Sell
        "size": size,
        # limitPrice/stopPrice/trailPrice omitted for Market
    }
    resp = requests.post(
        f"{PX_BASE}/api/Order/place",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json()

# ─── Webhook Endpoint ────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def tv_webhook():
    """
    Expected JSON body:
    {
      "symbol": "NQ",
      "signal": "BUY",  // or "SELL"
      "size": 1
    }
    """
    data = request.get_json()
    sym    = data.get("symbol")
    sig    = data.get("signal")
    size   = int(data.get("size", 1))

    if sig not in ("BUY", "SELL"):
        return jsonify({"error":"invalid signal"}), 400

    try:
        contract_id = search_contract(sym)
        result = place_order(contract_id, sig, size)
        return jsonify({"status":"ok","order":result})
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500

if __name__ == "__main__":
    # Pre-authenticate
    authenticate()
    app.run(host="0.0.0.0", port=TV_PORT)
