# tradingview_projectx_bot.py

import os
import time
import threading
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, time as dt_time
import pytz
from apscheduler.schedulers.background import BackgroundScheduler

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

# ─── Trading Hours (Central) ────────────────────────────
CT = pytz.timezone("US/Central")
TRADING_START = dt_time(17, 0)    # 5:00 PM CT
TRADING_END   = dt_time(15, 10)   # 3:10 PM CT (cutoff)

# ─── Flask app & globals ───────────────────────────────
app = Flask(__name__)
token = None
token_expiry = 0
lock = threading.Lock()

# ─── Auth Helpers ───────────────────────────────────────
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

# ─── Order & Position Helpers ──────────────────────────
def place_order(payload):
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Order/place",
        json=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    )
    resp.raise_for_status()
    return resp.json()

def place_market(cid, side, size):
    return place_order({"accountId": ACCOUNT_ID, "contractId": cid, "type": 2, "side": side, "size": size})

def place_limit(cid, side, size, price):
    return place_order({"accountId": ACCOUNT_ID, "contractId": cid, "type": 1, "side": side, "size": size, "limitPrice": price})

def place_stop(cid, side, size, price):
    return place_order({"accountId": ACCOUNT_ID, "contractId": cid, "type": 4, "side": side, "size": size, "stopPrice": price})

def search_open_orders():
    ensure_token()
    resp = requests.post(f"{PX_BASE}/api/Order/searchOpen", json={"accountId": ACCOUNT_ID},
                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json().get("orders", [])

def cancel_order(order_id):
    ensure_token()
    resp = requests.post(f"{PX_BASE}/api/Order/cancel", json={"accountId": ACCOUNT_ID, "orderId": order_id},
                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json()

def search_positions():
    ensure_token()
    resp = requests.post(f"{PX_BASE}/api/Position/searchOpen", json={"accountId": ACCOUNT_ID},
                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json().get("positions", [])

def close_position(cid):
    ensure_token()
    resp = requests.post(f"{PX_BASE}/api/Position/closeContract", json={"accountId": ACCOUNT_ID, "contractId": cid},
                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json()

def search_contract(symbol):
    ensure_token()
    resp = requests.post(f"{PX_BASE}/api/Contract/search", json={"searchText": symbol, "live": True},
                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    for c in resp.json().get("contracts", []):
        if c.get("activeContract"):
            return c["id"]
    raise ValueError(f"No active contract for symbol '{symbol}'")

# ─── Flatten (“Get Flat”) ──────────────────────────────
def flatten_all():
    for o in search_open_orders():
        cancel_order(o["orderId"])
    for p in search_positions():
        close_position(p["contractId"])
    app.logger.info("Flatten: all orders canceled and positions closed.")

# ─── Scheduler ─────────────────────────────────────────
def schedule_flatten():
    sched = BackgroundScheduler(timezone=CT)
    sched.add_job(flatten_all, 'cron', day_of_week='mon-fri', hour=15, minute=10)
    sched.start()
    app.logger.info("Scheduled flatten at 3:10 PM CT Mon–Fri.")

# ─── Webhook Endpoint ──────────────────────────────────
@app.route("/webhook", methods=["POST"])
def tv_webhook():
    now = datetime.now(CT).time()
    if not (TRADING_START <= now or now <= TRADING_END):
        return jsonify({"status":"skipped","message":"Outside trading hours"}), 200

    data = request.get_json()
    sig  = data.get("signal","").upper()

    if sig == "FLAT":
        flatten_all()
        return jsonify({"status":"ok","message":"Manual flatten"}), 200

    if sig not in ("BUY","SELL"):
        return jsonify({"error":"invalid signal"}), 400

    sym        = data.get("symbol")
    size_total = int(data.get("size",1))
    cid        = search_contract(sym)
    side       = 0 if sig=="BUY" else 1
    exit_side  = 1 - side

    # only cancel/close if opposite exists
    positions = [p for p in search_positions() if p["contractId"]==cid]
    has_opp = any((side==0 and p["size"]<0) or (side==1 and p["size"]>0) for p in positions)
    if has_opp:
        for o in search_open_orders():
            if o["contractId"]==cid:
                cancel_order(o["orderId"])
        for p in positions:
            if (side==0 and p["size"]<0) or (side==1 and p["size"]>0):
                close_position(cid)

    try:
        # entry
        entry = place_market(cid, side, size_total)
        fill_price = entry.get("fillPrice")
        if fill_price is None:
            raise RuntimeError("No fillPrice in entry response")

        # stop
        sl = (fill_price - STOP_LOSS_POINTS) if side==0 else (fill_price + STOP_LOSS_POINTS)
        place_stop(cid, exit_side, size_total, sl)

        # take profits
        n_tp       = len(TP_POINTS)
        base       = size_total // n_tp
        rem        = size_total - base*n_tp
        slices     = [base]*n_tp
        slices[-1]+= rem
        for pts, sz in zip(TP_POINTS, slices):
            tp = (fill_price + pts) if side==0 else (fill_price - pts)
            place_limit(cid, exit_side, sz, tp)

        return jsonify({"status":"ok","entry":entry}), 200

    except Exception as e:
        app.logger.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"status":"error","message":str(e)}), 500

# ─── Main ────────────────────────────────────────────────
if __name__ == "__main__":
    schedule_flatten()
    authenticate()
    app.run(host="0.0.0.0", port=TV_PORT)
