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
from logging.handlers import RotatingFileHandler

log_file = '/tmp/tradingview_projectx_bot.log'
file_handler = RotatingFileHandler(
    log_file, maxBytes=10*1024*1024, backupCount=5
)
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
file_handler.setFormatter(formatter)

# ─── Load config ───────────────────────────────────────
load_dotenv()
TV_PORT         = int(os.getenv("TV_PORT", 5000))
PX_BASE         = os.getenv("PROJECTX_BASE_URL")
USER_NAME       = os.getenv("PROJECTX_USERNAME")
API_KEY         = os.getenv("PROJECTX_API_KEY")
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET")
N8N_AI_URL      = "https://n8n.thetopham.com/webhook/5c793395-f218-4a49-a620-51d297f2dbfb"
N8N_AI_URL2      = "https://n8n.thetopham.com/webhook/fast"
SUPABASE_URL    = os.getenv("SUPABASE_URL")   # e.g. https://xxxx.supabase.co/rest/v1
SUPABASE_KEY    = os.getenv("SUPABASE_KEY")   # your Supabase API key

# Build account map from .env: any var ACCOUNT_<NAME>=<ID>
ACCOUNTS = {k[len("ACCOUNT_"):].lower(): int(v)
    for k, v in os.environ.items() if k.startswith("ACCOUNT_")}
DEFAULT_ACCOUNT = next(iter(ACCOUNTS), None)
if not ACCOUNTS:
    raise RuntimeError("No accounts loaded from .env. Add ACCOUNT_<NAME>=<ID>.")

AI_ENDPOINTS = {
    "epsilon": N8N_AI_URL,
    "beta": N8N_AI_URL2,
    # add more as needed
}


STOP_LOSS_POINTS = 10.0
TP_POINTS        = [2.5, 5.0, 10.0]
OVERRIDE_CONTRACT_ID = "CON.F.US.MES.M25"

CT = pytz.timezone("America/Chicago")
GET_FLAT_START = dtime(15, 7)
GET_FLAT_END   = dtime(17, 0)

session = requests.Session()
adapter = HTTPAdapter(pool_maxsize=10, max_retries=3)
session.mount("https://", adapter)

print("MODULE LOADED")
app = Flask(__name__)

if not any(isinstance(h, logging.Handler) and getattr(h, 'baseFilename', None) == log_file for h in app.logger.handlers):
    app.logger.addHandler(file_handler)
app.logger.setLevel(logging.INFO)
app.logger.propagate = False

if not any(isinstance(h, logging.Handler) and getattr(h, 'baseFilename', None) == log_file for h in logging.getLogger().handlers):
    logging.getLogger().addHandler(file_handler)
logging.getLogger().setLevel(logging.INFO)

app.logger.info("==== LOGGING SYSTEM READY ====")

_token = None
_token_expiry = 0
auth_lock = threading.Lock()

def in_get_flat(now=None):
    now = now or datetime.now(CT)
    t = now.timetz() if hasattr(now, "timetz") else now
    return GET_FLAT_START <= t <= GET_FLAT_END

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

def place_market(acct_id, cid, side, size):
    app.logger.info("Placing market order acct=%s cid=%s side=%s size=%s", acct_id, cid, side, size)
    return post("/api/Order/place", {
        "accountId": acct_id, "contractId": cid,
        "type": 2, "side": side, "size": size
    })

def place_limit(acct_id, cid, side, size, px):
    app.logger.info("Placing limit order acct=%s cid=%s size=%s px=%s", acct_id, cid, size, px)
    return post("/api/Order/place", {
        "accountId": acct_id, "contractId": cid,
        "type": 1, "side": side, "size": size, "limitPrice": px
    })

def place_stop(acct_id, cid, side, size, px):
    app.logger.info("Placing stop order acct=%s cid=%s size=%s px=%s", acct_id, cid, size, px)
    return post("/api/Order/place", {
        "accountId": acct_id, "contractId": cid,
        "type": 4, "side": side, "size": size, "stopPrice": px
    })

def search_open(acct_id):
    orders = post("/api/Order/searchOpen", {"accountId": acct_id}).get("orders", [])
    app.logger.debug("Open orders for %s: %s", acct_id, orders)
    return orders

def cancel(acct_id, order_id):
    resp = post("/api/Order/cancel", {"accountId": acct_id, "orderId": order_id})
    if not resp.get("success", True):
        app.logger.warning("Cancel reported failure: %s", resp)
    return resp

def search_pos(acct_id):
    pos = post("/api/Position/searchOpen", {"accountId": acct_id}).get("positions", [])
    app.logger.debug("Open positions for %s: %s", acct_id, pos)
    return pos

def close_pos(acct_id, cid):
    resp = post("/api/Position/closeContract", {"accountId": acct_id, "contractId": cid})
    if not resp.get("success", True):
        app.logger.warning("Close position reported failure: %s", resp)
    return resp

def search_trades(acct_id, since):
    trades = post("/api/Trade/search", {"accountId": acct_id, "startTimestamp": since.isoformat()}).get("trades", [])
    return trades

def flatten_contract(acct_id, cid, timeout=10):
    app.logger.info("Flattening contract %s for acct %s", cid, acct_id)
    end = time.time() + timeout
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
    while time.time() < end:
        rem_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]
        rem_pos    = [p for p in search_pos(acct_id) if p["contractId"] == cid]
        if not rem_orders and not rem_pos:
            app.logger.info("Flatten complete for %s", cid)
            return True
        app.logger.info("Waiting for flatten: %d orders, %d positions remain", len(rem_orders), len(rem_pos))
        time.sleep(1)
    app.logger.error("Flatten timeout: %s still has %d orders, %d positions", cid, len(rem_orders), len(rem_pos))
    return False

def cancel_all_stops(acct_id, cid):
    for o in search_open(acct_id):
        if o["contractId"] == cid and o["type"] == 4:
            cancel(acct_id, o["id"])

def get_contract(sym):
    if OVERRIDE_CONTRACT_ID:
        return OVERRIDE_CONTRACT_ID
    return None

def ai_trade_decision(account, strat, sig, sym, size, alert, ai_url):
    payload = {
        "account": account,
        "strategy": strat,
        "signal": sig,
        "symbol": sym,
        "size": size,
        "alert": alert
    }
    try:
        resp = session.post(ai_url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data
    except Exception as e:
        return {
            "strategy": strat,
            "signal": "HOLD",
            "account": account,
            "reason": f"AI error: {str(e)}",
            "error": True
        }

def check_for_phantom_orders(acct_id, cid):
    # 1. Check for open position(s)
    positions = [p for p in search_pos(acct_id) if p["contractId"] == cid]
    open_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]

    # 2. If there is an open position, make sure there are protective orders
    if positions:
        has_protective = any(o["type"] in (1, 4) and o["status"] == 1 for o in open_orders)
        if not has_protective:
            logging.warning(f"Phantom position detected! No stop/limit attached. Positions: {positions}, Orders: {open_orders}")
            flatten_contract(acct_id, cid, timeout=10)
    else:
        # 3. If there are no positions, but open stop/limit orders remain, cancel them
        leftover_orders = [o for o in open_orders if o["type"] in (1, 4) and o["status"] == 1]
        if leftover_orders:
            logging.warning(f"Leftover stop/limit order(s) found without a position! Orders: {leftover_orders}")
            for o in leftover_orders:
                try:
                    cancel(acct_id, o["id"])
                except Exception as e:
                    logging.error(f"Error cancelling phantom order {o['id']}: {e}")




# ─── Trade PnL Logging Helper ───────────────────────────────

def log_trade_results_to_supabase(acct_id, cid, entry_time, ai_decision_id, meta=None):
    """
    Fetches trades since entry_time from TopstepX API, sums profitAndLoss,
    and logs all info to Supabase.
    """
    meta = meta or {}
    # Fetch all trades for this contract since entry_time
    resp = post("/api/Trade/search", {
        "accountId": acct_id,
        "startTimestamp": entry_time.isoformat()
    })
    trades = resp.get("trades", [])

    total_pnl = sum(
        t["profitAndLoss"] or 0
        for t in trades
        if t.get("contractId") == cid and not t.get("voided", False)
    )
    exit_time = datetime.utcnow()
    payload = {
        "ai_decision_id": ai_decision_id,
        "entry_time": entry_time.isoformat(),
        "exit_time": exit_time.isoformat(),
        "duration_sec": int((exit_time - entry_time).total_seconds()),
        "total_pnl": total_pnl,
        "raw_trades": trades,
        **meta
    }
    url = f"{SUPABASE_URL}/rest/v1/trade_results"

    headers = {
        "apikey":       SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal"
    }
    r = session.post(url, json=payload, headers=headers, timeout=(3.05, 10))
    r.raise_for_status()

# ─── Bracket Strategy ─────────────────────────────────
def run_bracket(acct_id, sym, sig, size, alert, ai_decision_id=None):
    cid      = get_contract(sym)
    side     = 0 if sig=="BUY" else 1
    exit_side= 1-side
    pos = [p for p in search_pos(acct_id) if p["contractId"]==cid]

    if any((side==0 and p["type"]==1) or (side==1 and p["type"]==2) for p in pos):
        return jsonify(status="ok",strategy="bracket",message="skip same"),200

    if any((side==0 and p["type"]==2) or (side==1 and p["type"]==1) for p in pos):
        success = flatten_contract(acct_id, cid, timeout=10)
        if not success:
            return jsonify(status="error", message="Could not flatten contract—old orders/positions remain."), 500

    ent = place_market(acct_id, cid, side, size)
    oid = ent["orderId"]
    entry_time = datetime.utcnow()

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

    slp = price - STOP_LOSS_POINTS if side==0 else price + STOP_LOSS_POINTS
    sl  = place_stop(acct_id, cid, exit_side, size, slp)
    sl_id = sl["orderId"]

    tp_ids=[]
    n=len(TP_POINTS)
    base=size//n; rem=size-base*n
    slices=[base]*n; slices[-1]+=rem
    for pts,amt in zip(TP_POINTS,slices):
        px= price+pts if side==0 else price-pts
        r=place_limit(acct_id, cid, exit_side, amt, px)
        tp_ids.append(r["orderId"])

    def watcher():
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
                cancel_all_tps(); cancel_all_stops(acct_id, cid); break
            if not is_open(tp_ids[0]): break
            if not is_open(sl_id): cancel_all_tps(); cancel_all_stops(acct_id, cid); break
            time.sleep(4)
        cancel(acct_id, sl_id)
        new1 = place_stop(acct_id, cid, exit_side, slices[1]+slices[2], slp)
        st1 = new1["orderId"]
        while True:
            if is_flat():
                cancel_all_tps(); cancel_all_stops(acct_id, cid); break
            if not is_open(tp_ids[1]): break
            if not is_open(st1): cancel_all_tps(); cancel_all_stops(acct_id, cid); break
            time.sleep(4)
        cancel(acct_id, st1)
        slp2 = price - 5 if side == 0 else price + 5
        new2 = place_stop(acct_id, cid, exit_side, slices[2], slp2)
        st2 = new2["orderId"]
        while True:
            if is_flat():
                cancel_all_tps(); cancel_all_stops(acct_id, cid); break
            if not is_open(tp_ids[2]): break
            if not is_open(st2): cancel_all_tps(); cancel_all_stops(acct_id, cid); break
            time.sleep(4)
        cancel_all_tps(); cancel_all_stops(acct_id, cid)

        # --- LOGGING ---
        log_trade_results_to_supabase(
            acct_id=acct_id,
            cid=cid,
            entry_time=entry_time,
            ai_decision_id=ai_decision_id,
            meta={
                "order_id": oid,
                "symbol": sym,
                "account": acct_id,
                "strategy": "bracket",
                "signal": sig,
                "size": size,
                "alert": alert,
            }
        )
    threading.Thread(target=watcher,daemon=True).start()
    check_for_phantom_orders(acct_id, cid)
    return jsonify(status="ok",strategy="bracket",entry=ent),200

# ─── Brackmod Strategy (shorter SL/TP logic, with logging) ───────────
def run_brackmod(acct_id, sym, sig, size, alert, ai_decision_id=None):
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
    entry_time = datetime.utcnow()
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
                cancel_all_tps(); cancel_all_stops(acct_id, cid); break
            if not is_open(tp_ids[0]): break
            if not is_open(sl_id): cancel_all_tps(); cancel_all_stops(acct_id, cid); break
            time.sleep(4)
        cancel(acct_id, sl_id)
        new1 = place_stop(acct_id, cid, exit_side, slices[1], slp)
        st1 = new1["orderId"]
        while True:
            if is_flat():
                cancel_all_tps(); cancel_all_stops(acct_id, cid); break
            if not is_open(tp_ids[1]): break
            if not is_open(st1): cancel_all_tps(); cancel_all_stops(acct_id, cid); break
            time.sleep(4)
        cancel_all_tps(); cancel_all_stops(acct_id, cid)

        # --- LOGGING ---
        log_trade_results_to_supabase(
            acct_id=acct_id,
            cid=cid,
            entry_time=entry_time,
            ai_decision_id=ai_decision_id,
            meta={
                "order_id": oid,
                "symbol": sym,
                "account": acct_id,
                "strategy": "brackmod",
                "signal": sig,
                "size": size,
                "alert": alert,
            }
        )
    threading.Thread(target=watcher,daemon=True).start()
    check_for_phantom_orders(acct_id, cid)
    return jsonify(status="ok",strategy="brackmod",entry=ent),200

# ─── Pivot Strategy (sl, no tp, waits until next opposing signal) ───────────
def run_pivot(acct_id, sym, sig, size, alert, ai_decision_id=None):
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1
    exit_side = 1 - side
    pos = [p for p in search_pos(acct_id) if p["contractId"] == cid]
    net_pos = sum(p["size"] if p["type"] == 1 else -p["size"] for p in pos)
    target = size if sig == "BUY" else -size
    entry_time = datetime.utcnow()
    trade_log = []
    oid = None  # Track the entry order id for logging

    if net_pos == target:
        # No trade, but still log for completeness (optional)
        log_trade_results_to_supabase(
            acct_id=acct_id,
            cid=cid,
            entry_time=entry_time,
            ai_decision_id=ai_decision_id,
            meta={
                "order_id": oid,
                "symbol": sym,
                "account": acct_id,
                "strategy": "pivot",
                "signal": sig,
                "size": size,
                "alert": alert,
                "message": "already at target position"
            }
        )
        return jsonify(status="ok", strategy="pivot", message="already at target position"), 200

    # Cancel any existing stops
    for o in search_open(acct_id):
        if o["contractId"] == cid and o["type"] == 4:
            cancel(acct_id, o["id"])

    # If position is reversed, flatten then enter new
    if net_pos * target < 0:
        flatten_side = 1 if net_pos > 0 else 0
        trade_log.append(place_market(acct_id, cid, flatten_side, abs(net_pos)))
        ent = place_market(acct_id, cid, side, size)
        trade_log.append(ent)
        oid = ent.get("orderId")
    elif net_pos == 0:
        ent = place_market(acct_id, cid, side, size)
        trade_log.append(ent)
        oid = ent.get("orderId")
    elif abs(net_pos) != abs(target):
        flatten_side = 1 if net_pos > 0 else 0
        trade_log.append(place_market(acct_id, cid, flatten_side, abs(net_pos)))
        ent = place_market(acct_id, cid, side, size)
        trade_log.append(ent)
        oid = ent.get("orderId")

    # Place stop loss only (no TP)
    trades = [t for t in search_trades(acct_id, datetime.utcnow() - timedelta(minutes=5))
              if t["contractId"] == cid]
    entry_price = trades[-1]["price"] if trades else None
    if entry_price is not None:
        stop_price = entry_price - STOP_LOSS_POINTS if side == 0 else entry_price + STOP_LOSS_POINTS
        place_stop(acct_id, cid, exit_side, size, stop_price)

    # --- LOGGING ---
    log_trade_results_to_supabase(
        acct_id=acct_id,
        cid=cid,
        entry_time=entry_time,
        ai_decision_id=ai_decision_id,
        meta={
            "order_id": oid,
            "symbol": sym,
            "account": acct_id,
            "strategy": "pivot",
            "signal": sig,
            "size": size,
            "alert": alert,
            "trades": trade_log
        }
    )
    return jsonify(status="ok", strategy="pivot", message="position set", trades=trade_log), 200



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
    ai_decision_id = data.get("ai_decision_id", None)

    if acct not in ACCOUNTS:
        return jsonify(error=f"Unknown account '{acct}'"), 400

    acct_id = ACCOUNTS[acct]
    cid = get_contract(sym)

    if sig == "FLAT":
        ok = flatten_contract(acct_id, cid, timeout=10)
        status = "ok" if ok else "error"
        code = 200 if ok else 500
        return jsonify(status=status, strategy=strat, message="flattened"), code

    now = datetime.now(CT)
    if in_get_flat(now):
        return jsonify(status="ok", strategy=strat, message="in get-flat window, no trades"), 200

    # ----- Multi AI overseer logic -----
    if acct in AI_ENDPOINTS:
        ai_url = AI_ENDPOINTS[acct]
        ai_decision = ai_trade_decision(acct, strat, sig, sym, size, alert, ai_url)
        # If AI says HOLD or error, block trade
        if ai_decision.get("signal", "").upper() not in ("BUY", "SELL"):
            return jsonify(status="blocked", reason=ai_decision.get("reason", "No reason"), ai_decision=ai_decision), 200
        # Overwrite with AI's preferred strategy, symbol, etc.
        strat = ai_decision.get("strategy", strat)
        sig = ai_decision.get("signal", sig)
        sym = ai_decision.get("symbol", sym)
        size = ai_decision.get("size", size)
        alert = ai_decision.get("alert", alert)
        ai_decision_id = ai_decision.get("ai_decision_id", ai_decision_id)

    # ----- Continue for all accounts -----
    if strat == "bracket":
        return run_bracket(acct_id, sym, sig, size, alert, ai_decision_id)
    elif strat == "brackmod":
        return run_brackmod(acct_id, sym, sig, size, alert, ai_decision_id)
    elif strat == "pivot":
        return run_pivot(acct_id, sym, sig, size, alert, ai_decision_id)
    else:
        return jsonify(error=f"Unknown strategy '{strat}'"), 400

def phantom_order_sweeper():
    while True:
        for acct_name, acct_id in ACCOUNTS.items():
            cid = OVERRIDE_CONTRACT_ID
            try:
                check_for_phantom_orders(acct_id, cid)
            except Exception as e:
                logging.error(f"Error in phantom sweeper for {acct_name}: {e}")
        time.sleep(30)  # Sweep every 30s

# Start sweeper thread
threading.Thread(target=phantom_order_sweeper, daemon=True).start()


if __name__ == "__main__":
    app.logger.info("Starting  server.")
    app.run(host="0.0.0.0", port=TV_PORT)
