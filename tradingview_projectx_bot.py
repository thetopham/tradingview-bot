# tradingview_projectx_bot.py

import os, time, threading, requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timedelta

# ─── Load config ───────────────────────────────────────
load_dotenv()
TV_PORT    = int(os.getenv("TV_PORT", 5000))
PX_BASE    = os.getenv("PROJECTX_BASE_URL")
USER_NAME  = os.getenv("PROJECTX_USERNAME")
API_KEY    = os.getenv("PROJECTX_API_KEY")
ACCOUNT_ID = int(os.getenv("PROJECTX_ACCOUNT_ID"))

# Bracket params
STOP_LOSS_POINTS = 10.0
TP_POINTS        = [2.5, 5.0, 10.0]

app = Flask(__name__)
token = None
token_expiry = 0
lock = threading.Lock()

# ─── Auth & Token Management ───────────────────────────
def authenticate():
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
    with lock:
        if token is None or time.time() >= token_expiry:
            authenticate()

# ─── Low-level Order Calls ──────────────────────────────
def place_order(payload):
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Order/place",
        json=payload,
        headers={"Content-Type":"application/json", "Authorization":f"Bearer {token}"}
    )
    resp.raise_for_status()
    return resp.json()

def place_market(cid, side, size):
    return place_order({
        "accountId": ACCOUNT_ID, "contractId": cid,
        "type": 2,     # Market :contentReference[oaicite:4]{index=4}
        "side": side, "size": size
    })

def place_limit(cid, side, size, price):
    return place_order({
        "accountId": ACCOUNT_ID, "contractId": cid,
        "type": 1,      # Limit :contentReference[oaicite:5]{index=5}
        "side": side, "size": size, "limitPrice": price
    })

def place_stop(cid, side, size, price):
    return place_order({
        "accountId": ACCOUNT_ID, "contractId": cid,
        "type": 4,      # Stop :contentReference[oaicite:6]{index=6}
        "side": side, "size": size, "stopPrice": price
    })

# ─── Open Orders & Cancellation ────────────────────────
def search_open_orders():
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Order/searchOpen",
        json={"accountId": ACCOUNT_ID},
        headers={"Content-Type":"application/json", "Authorization":f"Bearer {token}"}
    )
    resp.raise_for_status()
    return resp.json().get("orders", [])  # orders[].id :contentReference[oaicite:7]{index=7}

def cancel_order(order_id):
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Order/cancel",
        json={"accountId": ACCOUNT_ID, "orderId": order_id},
        headers={"Content-Type":"application/json", "Authorization":f"Bearer {token}"}
    )
    resp.raise_for_status()
    return resp.json()

# ─── Position Management ───────────────────────────────
def search_positions():
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Position/searchOpen",
        json={"accountId": ACCOUNT_ID},
        headers={"Content-Type":"application/json", "Authorization":f"Bearer {token}"}
    )
    resp.raise_for_status()
    return resp.json().get("positions", [])  # positions[].type, positions[].size :contentReference[oaicite:8]{index=8}

def close_position(cid):
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Position/closeContract",
        json={"accountId": ACCOUNT_ID, "contractId": cid},
        headers={"Content-Type":"application/json", "Authorization":f"Bearer {token}"}
    )
    resp.raise_for_status()
    return resp.json()

# ─── Trade Lookup for Fill Price ───────────────────────
def search_trades(since: datetime):
    """Fetch trades since a given timestamp."""
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Trade/search",
        json={
            "accountId": ACCOUNT_ID,
            "startTimestamp": since.isoformat()
        },
        headers={"Content-Type":"application/json", "Authorization":f"Bearer {token}"}
    )
    resp.raise_for_status()
    return resp.json().get("trades", [])  # trades[].orderId, trades[].price :contentReference[oaicite:9]{index=9}

# ─── Contract Lookup ───────────────────────────────────
def search_contract(symbol: str) -> str:
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Contract/search",
        json={"searchText": symbol, "live": True},
        headers={"Content-Type":"application/json", "Authorization":f"Bearer {token}"}
    )
    resp.raise_for_status()
    for c in resp.json().get("contracts", []):
        if c.get("activeContract"):
            return c["id"]
    raise ValueError(f"No active contract for symbol '{symbol}'")

# ─── Webhook & Bracket Logic ──────────────────────────
@app.route("/webhook", methods=["POST"])
def tv_webhook():
    data       = request.get_json()
    sym        = data.get("symbol")
    sig        = data.get("signal","").upper()
    size_total = int(data.get("size",1))
    if sig not in ("BUY","SELL"):
        return jsonify({"error":"invalid signal"}),400

    cid       = search_contract(sym)
    side      = 0 if sig=="BUY" else 1
    exit_side = 1 - side

    # Only cancel/close if an OPPOSITE position exists (type=1 long, 2 short) :contentReference[oaicite:10]{index=10}
    positions = [p for p in search_positions() if p["contractId"] == cid]
    if side == 0:
        has_opp = any(p["type"] == 2 for p in positions)
    else:
        has_opp = any(p["type"] == 1 for p in positions)
    if has_opp:
        for o in search_open_orders():
            if o["contractId"] == cid:
                cancel_order(o["id"])
        for p in positions:
            if (side==0 and p["type"]==2) or (side==1 and p["type"]==1):
                close_position(cid)

    try:
        # 1) Market entry
        entry    = place_market(cid, side, size_total)
        order_id = entry["orderId"]
        # 2) Wait/lookup trades to compute fill price
        since     = datetime.utcnow() - timedelta(minutes=5)
        trades    = [t for t in search_trades(since) if t["orderId"] == order_id]
        total_sz  = sum(t["size"] for t in trades)
        fill_price = (sum(t["price"] * t["size"] for t in trades) / total_sz) if total_sz else None
        if fill_price is None:
            raise RuntimeError("Could not determine fill price from trades")

        # 3) Stop‐loss at fill±STOP_LOSS_POINTS
        sl = (fill_price - STOP_LOSS_POINTS) if side==0 else (fill_price + STOP_LOSS_POINTS)
        place_stop(cid, exit_side, size_total, sl)

        # 4) Take‐profits split across TP_POINTS
        n_tp       = len(TP_POINTS)
        base_slice = size_total // n_tp
        rem        = size_total - base_slice*n_tp
        slices     = [base_slice]*n_tp
        slices[-1] += rem
        for pts, sz in zip(TP_POINTS, slices):
            tp = (fill_price + pts) if side==0 else (fill_price - pts)
            place_limit(cid, exit_side, sz, tp)

        return jsonify({"status":"ok","entryOrder":entry}),200

    except Exception as e:
        app.logger.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"status":"error","message":str(e)}),500

if __name__ == "__main__":
    authenticate()
    app.run(host="0.0.0.0", port=TV_PORT)
