#!/usr/bin/env python3
# tradingview_projectx_bot.py

import os
import time
import threading
import requests
import re
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timedelta, time as dtime
import pytz

# ─── Load config ───────────────────────────────────────
load_dotenv()
TV_PORT   = int(os.getenv("TV_PORT", 5000))
PX_BASE   = os.getenv("PROJECTX_BASE_URL")
USER_NAME = os.getenv("PROJECTX_USERNAME")
API_KEY   = os.getenv("PROJECTX_API_KEY")

# Bracket params
STOP_LOSS_POINTS = 10.0
TP_POINTS        = [2.5, 5.0, 10.0]

# Hard-coded MES contract override
OVERRIDE_CONTRACT_ID = "CON.F.US.MES.M25"

# Central Time & get-flat window (3:10–5:00 PM CT)
CT = pytz.timezone("America/Chicago")
GET_FLAT_START = dtime(15, 10)
GET_FLAT_END   = dtime(17,  0)

app = Flask(__name__)
token = None
token_expiry = 0
lock = threading.Lock()
ACCOUNT_ID = None

# used to track TP and SL order IDs for the watcher
_active_watchers = {}  # orderId → Thread

def in_get_flat_zone(now=None):
    """Return True if `now` is between GET_FLAT_START and GET_FLAT_END CT."""
    if now is None:
        now = datetime.now(CT)
    t = now.timetz() if isinstance(now, datetime) else now
    return GET_FLAT_START <= t <= GET_FLAT_END

# ─── Authentication ────────────────────────────────────
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
    token_expiry = time.time() + 23*3600

def ensure_token():
    with lock:
        if token is None or time.time() >= token_expiry:
            authenticate()

def post(path, payload):
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}{path}",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json()

# ─── Order / Position / Trade Helpers ─────────────────
def place_market(cid, side, size):      return post("/api/Order/place", {"accountId": ACCOUNT_ID, "contractId": cid, "type":2, "side":side, "size":size})
def place_limit(cid, side, size, p):    return post("/api/Order/place", {"accountId": ACCOUNT_ID, "contractId": cid, "type":1, "side":side, "size":size, "limitPrice":p})
def place_stop(cid, side, size, p):     return post("/api/Order/place", {"accountId": ACCOUNT_ID, "contractId": cid, "type":4, "side":side, "size":size, "stopPrice":p})
def search_open_orders():               return post("/api/Order/searchOpen", {"accountId": ACCOUNT_ID}).get("orders", [])
def cancel_order(oid):                  return post("/api/Order/cancel", {"accountId": ACCOUNT_ID, "orderId":oid})
def search_positions():                 return post("/api/Position/searchOpen", {"accountId": ACCOUNT_ID}).get("positions", [])
def close_position(cid):                return post("/api/Position/closeContract", {"accountId": ACCOUNT_ID, "contractId":cid})
def search_trades(since):               return post("/api/Trade/search", {"accountId": ACCOUNT_ID, "startTimestamp": since.isoformat()}).get("trades", [])

# ─── Contract Lookup ───────────────────────────────────
def _lookup_raw(raw):
    ctrs = post("/api/Contract/search", {"searchText": raw, "live": True}).get("contracts", [])
    for c in ctrs:
        if c.get("activeContract"):
            return c["id"]
    if ctrs:
        return ctrs[0]["id"]
    raise ValueError(f"No contract for code '{raw}'")

def search_contract(sym):
    if OVERRIDE_CONTRACT_ID:
        return OVERRIDE_CONTRACT_ID
    if sym.upper().startswith("CON."):
        return _lookup_raw(sym)
    root = re.match(r"^([A-Za-z]+)", sym).group(1)
    return _lookup_raw(root)

# ─── Pick & Cache Account ─────────────────────────────
@app.before_request
def pick_account():
    global ACCOUNT_ID
    if token is None:
        authenticate()
    if ACCOUNT_ID is None:
        accts = post("/api/Account/search", {"onlyActiveAccounts": True}).get("accounts", [])
        ACCOUNT_ID = int(os.getenv("PROJECTX_ACCOUNT_ID") or accts[0]["id"])
        app.logger.info(f"Using Account {ACCOUNT_ID}")

# ─── TP₂ Watcher ──────────────────────────────────────
def _watch_tp2_break_even(cid, exit_side, total_size, entry_price, stop_order_id, tp2_order_id):
    """Poll every 5s; when TP₂ fills, replace stop at break-even."""
    while True:
        time.sleep(5)
        open_ids = {o["id"] for o in search_open_orders()}
        # if TP2 is no longer open → it filled
        if tp2_order_id not in open_ids:
            # cancel old stop
            if stop_order_id in open_ids:
                cancel_order(stop_order_id)
            # place new stop at entry_price
            new = place_stop(cid, exit_side, total_size, entry_price)
            app.logger.info(f"Moved SL to BE @ {entry_price}, new stop ID {new['orderId']}")
            return

# ─── Webhook Endpoint ───────────────────────────────────
@app.route("/webhook", methods=["POST"])
def tv_webhook():
    data = request.get_json()
    sig  = data.get("signal", "").upper()
    sym  = data.get("symbol", "")
    size = int(data.get("size", 1))
    now  = datetime.now(CT)

    if sig not in ("BUY","SELL","FLAT"):
        return jsonify(error="invalid signal"), 400

    # flatten on FLAT signal or during get-flat window
    if sig == "FLAT" or in_get_flat_zone(now):
        for o in search_open_orders(): cancel_order(o["id"])
        for p in search_positions():    close_position(p["contractId"])
        return jsonify(status="ok", message="flattened"), 200

    # entry direction
    side      = 0 if sig=="BUY" else 1
    exit_side = 1 - side
    cid       = search_contract(sym)

    # fetch current positions
    pos = [p for p in search_positions() if p["contractId"]==cid]

    # skip if same-side already open
    if any((side==0 and p["type"]==1) or (side==1 and p["type"]==2) for p in pos):
        return jsonify(status="ok", message="already open same side"),200

    # flatten opposing
    if any((side==0 and p["type"]==2) or (side==1 and p["type"]==1) for p in pos):
        for o in search_open_orders(): cancel_order(o["id"])
        for p in pos: close_position(p["contractId"])

    # 1) Market entry
    ent = place_market(cid, side, size)
    oid = ent["orderId"]

    # 2) Compute fill price from recent trades
    trades = [t for t in search_trades(datetime.utcnow()-timedelta(minutes=5)) if t["orderId"]==oid]
    tot    = sum(t["size"] for t in trades)
    price  = (sum(t["price"]*t["size"] for t in trades)/tot) if tot else ent.get("fillPrice")

    # 3) Initial stop-loss
    sl_price = price - STOP_LOSS_POINTS if side==0 else price + STOP_LOSS_POINTS
    sl_resp  = place_stop(cid, exit_side, size, sl_price)
    sl_id     = sl_resp["orderId"]

    # 4) Place TP legs and capture their IDs
    tp_ids = []
    n_tp    = len(TP_POINTS)
    base    = size // n_tp
    rem     = size - base*n_tp
    slices  = [base]*n_tp; slices[-1] += rem

    for pts,amt in zip(TP_POINTS, slices):
        tp_px = (price + pts) if side==0 else (price - pts)
        r     = place_limit(cid, exit_side, amt, tp_px)
        tp_ids.append(r["orderId"])

    # 5) Launch watcher for TP₂ → break-even
    if len(tp_ids) >= 2:
        t2 = tp_ids[1]
        thr = threading.Thread(
            target=_watch_tp2_break_even,
            args=(cid, exit_side, size, price, sl_id, t2),
            daemon=True
        )
        thr.start()
        _active_watchers[t2] = thr

    return jsonify(status="ok", entry=ent), 200

# ─── Boot ───────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=TV_PORT)
