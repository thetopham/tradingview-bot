# tradingview_projectx_bot.py

import os
import time
import threading
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timezone

# ─── Load configuration from .env ───────────────────────
load_dotenv()

TV_PORT          = int(os.getenv("TV_PORT", 5000))
PX_BASE          = os.getenv("PROJECTX_BASE_URL")
USER_NAME        = os.getenv("PROJECTX_USERNAME")
API_KEY          = os.getenv("PROJECTX_API_KEY")
ACCOUNT_ID       = int(os.getenv("PROJECTX_ACCOUNT_ID"))

# Bracket parameters (in price units)
STOP_LOSS_POINTS = 10.0
TP_POINTS        = [2.5, 5.0, 10.0]

# ─── Flask app & globals ───────────────────────────────
app = Flask(__name__)
token = None
token_expiry = 0
lock = threading.Lock()

# ─── Authentication Helpers ────────────────────────────
def authenticate():
    """Obtain a new JWT token via loginKey."""
    global token, token_expiry
    resp = requests.post(
        f"{PX_BASE}/api/Auth/loginKey",
        json={"userName": USER_NAME, "apiKey": API_KEY},
        headers={"Content-Type": "application/json"}
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Auth failed: {data}")
    token = data["token"]
    token_expiry = time.time() + 23 * 3600

def ensure_token():
    """Refresh JWT token if missing or expired."""
    with lock:
        if token is None or time.time() >= token_expiry:
            authenticate()

# ─── Order Placement Helpers ───────────────────────────
def place_order(payload):
    ensure_token()
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

def place_market(contract_id, side, size):
    return place_order({
        "accountId": ACCOUNT_ID,
        "contractId": contract_id,
        "type": 2,      # Market 
        "side": side,
        "size": size
    })

def place_limit(contract_id, side, size, limit_price):
    return place_order({
        "accountId": ACCOUNT_ID,
        "contractId": contract_id,
        "type": 1,          # Limit 
        "side": side,
        "size": size,
        "limitPrice": limit_price
    })

def place_stop(contract_id, side, size, stop_price):
    return place_order({
        "accountId": ACCOUNT_ID,
        "contractId": contract_id,
        "type": 4,          # Stop 
        "side": side,
        "size": size,
        "stopPrice": stop_price
    })

# ─── Open Orders / Cancel Helpers ──────────────────────
def search_open_orders():
    """Fetch all open orders for this account."""
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Order/searchOpen",
        json={"accountId": ACCOUNT_ID},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json().get("orders", [])

def cancel_order(order_id):
    """Cancel a single open order by ID."""
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Order/cancel",
        json={"accountId": ACCOUNT_ID, "orderId": order_id},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json()

# ─── Position Helpers ──────────────────────────────────
def search_positions():
    """Fetch all open positions for this account."""
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Position/searchOpen",
        json={"accountId": ACCOUNT_ID},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json().get("positions", [])  # positions[].size 

def close_position(contract_id):
    """Close the entire position for a given contract."""
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Position/closeContract",
        json={"accountId": ACCOUNT_ID, "contractId": contract_id},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json()

# ─── Trade Helpers ────────────────────────────────────
def search_trades():
    """Fetch recent trades for this account."""
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Trade/search",
        json={"accountId": ACCOUNT_ID},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json().get("trades", [])  # trades[].orderId, trades[].price :contentReference[oaicite:7]{index=7}

# ─── Contract Lookup ───────────────────────────────────
def search_contract(symbol: str) -> str:
    """Find the active contract ID for a given symbol."""
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
    for c in resp.json().get("contracts", []):
        if c.get("activeContract"):
            return c["id"]
    raise ValueError(f"No active contract for symbol '{symbol}'")

# ─── Webhook Endpoint ──────────────────────────────────
@app.route("/webhook", methods=["POST"])
def tv_webhook():
    data = request.get_json()
    sym        = data.get("symbol")
    sig        = data.get("signal", "").upper()
    size_total = int(data.get("size", 1))

    if sig not in ("BUY", "SELL"):
        return jsonify({"error": "invalid signal"}), 400

    contract_id = search_contract(sym)
    side        = 0 if sig == "BUY" else 1
    exit_side   = 1 - side

    # ── Cancel/Close only on an OPPOSITE position ───────────
    positions = [p for p in search_positions() if p["contractId"] == contract_id]
    if side == 0:
        has_opp = any(p["size"] < 0 for p in positions)
    else:
        has_opp = any(p["size"] > 0 for p in positions)

    if has_opp:
        for o in search_open_orders():
            if o.get("contractId") == contract_id:
                cancel_order(o["orderId"])
        for p in positions:
            if (side == 0 and p["size"] < 0) or (side == 1 and p["size"] > 0):
                close_position(contract_id)
    # ─────────────────────────────────────────────────────────

    try:
        # 1) Entry
        entry = place_market(contract_id, side, size_total)
        order_id = entry["orderId"]

        # 2) Fetch trades & compute weighted fill price
        trades = [t for t in search_trades() if t["orderId"] == order_id]
        total_sz = sum(t["size"] for t in trades)
        fill_price = (
            sum(t["price"] * t["size"] for t in trades) / total_sz
            if total_sz else None
        )
        if fill_price is None:
            raise RuntimeError("Could not determine fill price from trades")

        # 3) Stop-loss
        sl_price = (fill_price - STOP_LOSS_POINTS) if side == 0 else (fill_price + STOP_LOSS_POINTS)
        place_stop(contract_id, exit_side, size_total, sl_price)

        # 4) Split total size across TPs
        n_tp       = len(TP_POINTS)
        base_slice = size_total // n_tp
        rem        = size_total - base_slice * n_tp
        slices     = [base_slice] * n_tp
        slices[-1] += rem

        for pts, sz in zip(TP_POINTS, slices):
            tp_price = (fill_price + pts) if side == 0 else (fill_price - pts)
            place_limit(contract_id, exit_side, sz, tp_price)

        return jsonify({"status": "ok", "entryOrder": entry}), 200

    except Exception as e:
        app.logger.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# ─── Main ────────────────────────────────────────────────
if __name__ == "__main__":
    authenticate()
    app.run(host="0.0.0.0", port=TV_PORT)
