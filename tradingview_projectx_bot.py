#!/usr/bin/env python3
# tradingview_projectx_bot.py

import os
import time
import threading
import requests
from requests.adapters import HTTPAdapter
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timedelta, time as dtime
import pytz
import logging

# ─── Load config ───────────────────────────────────────
load_dotenv()
TV_PORT       = int(os.getenv("TV_PORT", 5000))
PX_BASE       = os.getenv("PROJECTX_BASE_URL")
USER_NAME     = os.getenv("PROJECTX_USERNAME")
API_KEY       = os.getenv("PROJECTX_API_KEY")
WEBHOOK_SECRET= os.getenv("WEBHOOK_SECRET")
N8N_AI_URL    = "https://n8n.thetopham.com/webhook/5c793395-f218-4a49-a620-51d297f2dbfb"

# Build account map from .env: any var ACCOUNT_<NAME>=<ID>
ACCOUNTS = {k[len("ACCOUNT_"):].lower(): int(v)
    for k, v in os.environ.items() if k.startswith("ACCOUNT_")}
DEFAULT_ACCOUNT = next(iter(ACCOUNTS), None)
if not ACCOUNTS:
    raise RuntimeError("No accounts loaded from .env. Add ACCOUNT_<NAME>=<ID>.")

# Bracket params
STOP_LOSS_POINTS = 10.0
TP_POINTS        = [2.5, 5.0, 10.0]

# Hard-coded MES override
OVERRIDE_CONTRACT_ID = "CON.F.US.MES.M25"

# Central Time & Get-Flat window
CT = pytz.timezone("America/Chicago")
GET_FLAT_START = dtime(15, 7)
GET_FLAT_END   = dtime(17, 0)

# ─── HTTP session with keep-alive & retries ────────────
session = requests.Session()
adapter = HTTPAdapter(pool_maxsize=10, max_retries=3)
session.mount("https://", adapter)

print("MODULE LOADED")
app = Flask(__name__)

gunicorn_logger = logging.getLogger('gunicorn.error')
if gunicorn_logger.handlers:
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
else:
    app.logger.setLevel(logging.INFO)

_token = None
_token_expiry = 0
auth_lock = threading.Lock()

def in_get_flat(now=None):
    now = now or datetime.now(CT)
    t = now.timetz() if hasattr(now, "timetz") else now
    return GET_FLAT_START <= t <= GET_FLAT_END

# ─── Auth & HTTP ───────────────────────────────────────
def authenticate():
    global _token, _token_expiry
    app.logger.info("Authenticating to Topstep API...")
    resp = session.post(
        f"{PX_BASE}/api/Auth/loginKey",
        json={"userName": USER_NAME, "apiKey": API_KEY},
        headers={"Content-Type": "application/json"},
        timeout=(3.05, 10)
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        app.logger.error("Auth failed: %s", data)
        raise RuntimeError("Auth failed")
    _token = data["token"]
    _token_expiry = time.time() + 23 * 3600
    app.logger.info("Authentication successful; token expires in ~23h.")

def ensure_token():
    with auth_lock:
        if _token is None or time.time() >= _token_expiry:
            authenticate()

def post(path, payload):
    ensure_token()
    url = f"{PX_BASE}{path}"
    app.logger.info("POST %s payload=%s", url, payload)
    resp = session.post(
        url,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_token}"
        },
        timeout=(3.05, 10)
    )
    if resp.status_code == 429:
        app.logger.warning("Rate limit hit: %s %s", resp.status_code, resp.text)
    resp.raise_for_status()
    data = resp.json()
    app.logger.debug("Response JSON: %s", data)
    return data

# ─── Order/Pos/Trade Helpers ──────────────────────────
def place_market(acct_id, cid, side, size):
    app.logger.info("Placing market order acct=%s cid=%s side=%s size=%s",
                    acct_id, cid, side, size)
    return post("/api/Order/place",
                {"accountId": acct_id, "contractId": cid,
                 "type": 2, "side": side, "size": size})

def place_limit(acct_id, cid, side, size, px):
    app.logger.info("Placing limit order acct=%s cid=%s size=%s px=%s",
                    acct_id, cid, size, px)
    return post("/api/Order/place",
                {"accountId": acct_id, "contractId": cid,
                 "type": 1, "side": side, "size": size, "limitPrice": px})

def place_stop(acct_id, cid, side, size, px):
    app.logger.info("Placing stop order acct=%s cid=%s size=%s px=%s",
                    acct_id, cid, size, px)
    return post("/api/Order/place",
                {"accountId": acct_id, "contractId": cid,
                 "type": 4, "side": side, "size": size, "stopPrice": px})

def search_open(acct_id):
    orders = post("/api/Order/searchOpen",
                  {"accountId": acct_id}).get("orders", [])
    app.logger.debug("Open orders for %s: %s", acct_id, orders)
    return orders

def cancel(acct_id, order_id):
    resp = post("/api/Order/cancel",
                {"accountId": acct_id, "orderId": order_id})
    if not resp.get("success", True):
        app.logger.warning("Cancel reported failure: %s", resp)
    return resp

def search_pos(acct_id):
    pos = post("/api/Position/searchOpen",
               {"accountId": acct_id}).get("positions", [])
    app.logger.debug("Open positions for %s: %s", acct_id, pos)
    return pos

def close_pos(acct_id, cid):
    resp = post("/api/Position/closeContract",
                {"accountId": acct_id, "contractId": cid})
    if not resp.get("success", True):
        app.logger.warning("Close position reported failure: %s", resp)
    return resp

def search_trades(acct_id, since):
    trades = post("/api/Trade/search",
                  {"accountId": acct_id, "startTimestamp": since.isoformat()}).get("trades", [])
    return trades

# ─── Robust flatten ────────────────────────────────────
def flatten_contract(acct_id, cid, timeout=10):
    app.logger.info("Flattening contract %s for acct %s", cid, acct_id)
    end = time.time() + timeout

    # Cancel all orders
    while time.time() < end:
        open_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]
        if not open_orders:
            break
        for o in open_orders:
            try:
                cancel(acct_id, o["id"])
            except Exception as e:
                app.logger.error("Error cancelling %s: %s", o["id"], e)
        time.sleep(1)

    # Close all positions
    while time.time() < end:
        positions = [p for p in search_pos(acct_id) if p["contractId"] == cid]
        if not positions:
            break
        for _ in positions:
            try:
                close_pos(acct_id, cid)
            except Exception as e:
                app.logger.error("Error closing position %s: %s", cid, e)
        time.sleep(1)

    # Final polling
    while time.time() < end:
        rem_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]
        rem_pos    = [p for p in search_pos(acct_id) if p["contractId"] == cid]
        if not rem_orders and not rem_pos:
            app.logger.info("Flatten complete for %s", cid)
            return True
        app.logger.info("Waiting for flatten: %d orders, %d positions remain",
                        len(rem_orders), len(rem_pos))
        time.sleep(1)

    app.logger.error("Flatten timeout: %s still has %d orders, %d positions",
                     cid, len(rem_orders), len(rem_pos))
    return False

def cancel_all_stops(acct_id, cid):
    for o in search_open(acct_id):
        if o["contractId"] == cid and o["type"] == 4:
            cancel(acct_id, o["id"])

# ─── Contract Lookup ───────────────────────────────────
def get_contract(sym):
    if OVERRIDE_CONTRACT_ID:
        return OVERRIDE_CONTRACT_ID
    return None

# ─── LLM (AI Vision, via n8n) Filter for Epsilon ──────
def ai_trade_decision(account, strat, sig, sym, size):
    payload = {
        "account": account,
        "strategy": strat,
        "signal": sig,
        "symbol": sym,
        "size": size
    }
    try:
        resp = session.post(N8N_AI_URL, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        dec = str(data.get('action', '')).upper()
        return dec in ("BUY", "SELL"), data.get('reason', ''), data.get('chart_url', '')
    except Exception as e:
        return False, f"AI error: {str(e)}", None

# ─── Bracket Strategy ─────────────────────────────────
def run_bracket(acct_id, sym, sig, size):
    cid      = get_contract(sym)
    side     = 0 if sig=="BUY" else 1
    exit_side= 1-side
    pos = [p for p in search_pos(acct_id) if p["contractId"]==cid]

    # skip same side
    if any((side==0 and p["type"]==1) or (side==1 and p["type"]==2) for p in pos):
        return jsonify(status="ok",strategy="bracket",message="skip same"),200

    # flatten opposite (SAFE FLIP)
    if any((side==0 and p["type"]==2) or (side==1 and p["type"]==1) for p in pos):
        success = flatten_contract(acct_id, cid, timeout=10)
        if not success:
            return jsonify(status="error", message="Could not flatten contract—old orders/positions remain."), 500

    # market entry
    ent = place_market(acct_id, cid, side, size)
    oid = ent["orderId"]

    # fill price
    price = None
    for _ in range(12):
        trades = [t for t in search_trades(acct_id, datetime.utcnow()-timedelta(minutes=5)) if t["orderId"]==oid]
        tot = sum(t["size"] for t in trades)
        if tot:
            price = sum(t["price"]*t["size"] for t in trades)/tot
            break
        price = ent.get("fillPrice")
        if price is not None:
            break
        time.sleep(1)

    if price is None:
        return jsonify(status="error", message="No fill price available"), 500

    # initial SL
    slp = price - STOP_LOSS_POINTS if side==0 else price + STOP_LOSS_POINTS
    sl  = place_stop(acct_id, cid, exit_side, size, slp)
    sl_id = sl["orderId"]

    # TP legs
    tp_ids=[]
    n=len(TP_POINTS)
    base=size//n; rem=size-base*n
    slices=[base]*n; slices[-1]+=rem
    for pts,amt in zip(TP_POINTS,slices):
        px= price+pts if side==0 else price-pts
        r=place_limit(acct_id, cid, exit_side, amt, px)
        tp_ids.append(r["orderId"])

    # Watcher
    def watcher():
        a, b, c = slices
        def is_open(order_id):
            return order_id in {o["id"] for o in search_open(acct_id)}
        def cancel_all_tps():
            for o in search_open(acct_id):
                if o["contractId"] == cid and o["type"] == 1 and o["id"] in tp_ids:
                    cancel(acct_id, o["id"])
        def is_flat():
            return not any(p for p in search_pos(acct_id) if p["contractId"] == cid)
        # Wait for TP1 or SL
        while True:
            if is_flat():
                cancel_all_tps(); cancel_all_stops(acct_id, cid); return
            if not is_open(tp_ids[0]): break
            if not is_open(sl_id): cancel_all_tps(); cancel_all_stops(acct_id, cid); return
            time.sleep(4)
        cancel(acct_id, sl_id)
        new1 = place_stop(acct_id, cid, exit_side, b + c, slp)
        st1 = new1["orderId"]
        # Wait for TP2 or SL
        while True:
            if is_flat():
                cancel_all_tps(); cancel_all_stops(acct_id, cid); return
            if not is_open(tp_ids[1]): break
            if not is_open(st1): cancel_all_tps(); cancel_all_stops(acct_id, cid); return
            time.sleep(4)
        cancel(acct_id, st1)
        slp2 = price - 5 if side == 0 else price + 5
        new2 = place_stop(acct_id, cid, exit_side, c, slp2)
        st2 = new2["orderId"]
        # Wait for TP3 or SL
        while True:
            if is_flat():
                cancel_all_tps(); cancel_all_stops(acct_id, cid); return
            if not is_open(tp_ids[2]): break
            if not is_open(st2): cancel_all_tps(); cancel_all_stops(acct_id, cid); return
            time.sleep(4)
        cancel_all_tps(); cancel_all_stops(acct_id, cid)
    threading.Thread(target=watcher,daemon=True).start()
    return jsonify(status="ok",strategy="bracket",entry=ent),200

# ─── Brackmod Strategy (shorter SL/TP logic) ───────────
def run_brackmod(acct_id, sym, sig, size):
    cid      = get_contract(sym)
    side     = 0 if sig=="BUY" else 1
    exit_side= 1-side
    pos = [p for p in search_pos(acct_id) if p["contractId"]==cid]
    if any((side==0 and p["type"]==1) or (side==1 and p["type"]==2) for p in pos):
        return jsonify(status="ok",strategy="brackmod",message="skip same"),200
    if any((side==0 and p["type"]==2) or (side==1 and p["type"]==1) for p in pos):
        success = flatten_contract(acct_id, cid, timeout=10)
        if not success:
            return jsonify(status="error", message="Could not flatten contract—old orders/positions remain."), 500
    ent = place_market(acct_id, cid, side, size)
    oid = ent["orderId"]
    # fill price
    price = None
    for _ in range(12):
        trades = [t for t in search_trades(acct_id, datetime.utcnow()-timedelta(minutes=5)) if t["orderId"]==oid]
        tot = sum(t["size"] for t in trades)
        if tot:
            price = sum(t["price"]*t["size"] for t in trades)/tot
            break
        price = ent.get("fillPrice")
        if price is not None:
            break
        time.sleep(1)
    if price is None:
        return jsonify(status="error", message="No fill price available"), 500
    STOP_LOSS_POINTS = 5.75
    slp = price - STOP_LOSS_POINTS if side==0 else price + STOP_LOSS_POINTS
    sl  = place_stop(acct_id, cid, exit_side, size, slp)
    sl_id = sl["orderId"]
    TP_POINTS = [2.5, 5.0]
    slices = [2, 1]
    tp_ids=[]
    for pts, amt in zip(TP_POINTS, slices):
        px = price + pts if side == 0 else price - pts
        r = place_limit(acct_id, cid, exit_side, amt, px)
        tp_ids.append(r["orderId"])
    def watcher():
        a, b = slices
        def is_open(order_id):
            return order_id in {o["id"] for o in search_open(acct_id)}
        def cancel_all_tps():
            for o in search_open(acct_id):
                if o["contractId"] == cid and o["type"] == 1 and o["id"] in tp_ids:
                    cancel(acct_id, o["id"])
        def is_flat():
            return not any(p for p in search_pos(acct_id) if p["contractId"] == cid)
        while True:
            if is_flat():
                cancel_all_tps(); cancel_all_stops(acct_id, cid); return
            if not is_open(tp_ids[0]): break
            if not is_open(sl_id): cancel_all_tps(); cancel_all_stops(acct_id, cid); return
            time.sleep(4)
        cancel(acct_id, sl_id)
        new1 = place_stop(acct_id, cid, exit_side, b, slp)
        st1 = new1["orderId"]
        while True:
            if is_flat():
                cancel_all_tps(); cancel_all_stops(acct_id, cid); return
            if not is_open(tp_ids[1]): break
            if not is_open(st1): cancel_all_tps(); cancel_all_stops(acct_id, cid); return
            time.sleep(4)
        cancel_all_tps(); cancel_all_stops(acct_id, cid)
    threading.Thread(target=watcher,daemon=True).start()
    return jsonify(status="ok",strategy="brackmod",entry=ent),200

# ─── Pivot Strategy ────────────────────────────────────
def run_pivot(acct_id, sym, sig, size):
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1
    exit_side = 1 - side
    pos = [p for p in search_pos(acct_id) if p["contractId"] == cid]
    net_pos = sum(p["size"] if p["type"] == 1 else -p["size"] for p in pos)
    target = size if sig == "BUY" else -size
    if net_pos == target:
        return jsonify(status="ok", strategy="pivot", message="already at target position"), 200
    for o in search_open(acct_id):
        if o["contractId"] == cid and o["type"] == 4:
            cancel(acct_id, o["id"])
    trade_log = []
    if net_pos * target < 0:
        flatten_side = 1 if net_pos > 0 else 0
        trade_log.append(place_market(acct_id, cid, flatten_side, abs(net_pos)))
        trade_log.append(place_market(acct_id, cid, side, size))
    elif net_pos == 0:
        trade_log.append(place_market(acct_id, cid, side, size))
    elif abs(net_pos) != abs(target):
        flatten_side = 1 if net_pos > 0 else 0
        trade_log.append(place_market(acct_id, cid, flatten_side, abs(net_pos)))
        trade_log.append(place_market(acct_id, cid, side, size))
    trades = [t for t in search_trades(acct_id, datetime.utcnow() - timedelta(minutes=5))
              if t["contractId"] == cid]
    entry_price = trades[-1]["price"] if trades else None
    if entry_price is not None:
        stop_price = entry_price - STOP_LOSS_POINTS if side == 0 else entry_price + STOP_LOSS_POINTS
        place_stop(acct_id, cid, exit_side, size, stop_price)
    return jsonify(status="ok", strategy="pivot", message="position set", trades=trade_log), 200

# ─── Webhook Dispatcher ─────────────────────────────────
@app.route("/webhook", methods=["POST"])
def tv_webhook():
    data = request.get_json()
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="unauthorized"), 403
    strat = data.get("strategy", "bracket").lower()
    acct  = (data.get("account") or DEFAULT_ACCOUNT).lower()
    sig   = data.get("signal", "").upper()
    sym   = data.get("symbol", "")
    size  = int(data.get("size", 1))
    alert = data.get("alert", "")
    if acct not in ACCOUNTS:
        return jsonify(error=f"Unknown account '{acct}'"), 400
    acct_id = ACCOUNTS[acct]
    cid = get_contract(sym)
    # Explicit FLAT: flatten and return
    if sig == "FLAT":
        ok = flatten_contract(acct_id, cid, timeout=10)
        status = "ok" if ok else "error"
        code = 200 if ok else 500
        return jsonify(status=status, strategy=strat, message="flattened"), code
    now = datetime.now(CT)
    if in_get_flat(now):
        return jsonify(status="ok", strategy=strat, message="in get-flat window, no trades"), 200
    # AI check for epsilon account
    if acct == "epsilon":
        allow, reason, chart_url = ai_trade_decision(acct, strat, sig, sym, size)
        if not allow:
            return jsonify(status="blocked", reason=reason, chart=chart_url), 200
    if strat == "bracket":
        return run_bracket(acct_id, sym, sig, size)
    elif strat == "brackmod":
        return run_brackmod(acct_id, sym, sig, size)
    elif strat == "pivot":
        return run_pivot(acct_id, sym, sig, size)
    else:
        return jsonify(error=f"Unknown strategy '{strat}'"), 400

if __name__ == "__main__":
    app.logger.info("Starting tradingview_projectx_bot server.")
    app.run(host="0.0.0.0", port=TV_PORT)
