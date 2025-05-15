#!/usr/bin/env python3
# tradingview_projectx_bot.py

import os, time, threading, requests, re
from flask import Flask, request, jsonify, g
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
N8N_AI_URL    = "https://n8n.thetopham.com/webhook/5c793395-f218-4a49-a620-51d297f2dbfb"

# Build account map from .env: any var ACCOUNT_<NAME>=<ID>
ACCOUNTS = {}
for k,v in os.environ.items():
    if k.startswith("ACCOUNT_"):
        ACCOUNTS[k[len("ACCOUNT_"):].lower()] = int(v)
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
GET_FLAT_START = dtime(15,7)
GET_FLAT_END   = dtime(17,0)

logging.basicConfig(level=logging.INFO)
print("MODULE LOADED")
app = Flask(__name__)
app.logger.propagate = True
_token = None
_token_expiry = 0
lock = threading.Lock()


gunicorn_logger = logging.getLogger('gunicorn.error')
if gunicorn_logger.handlers:
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
else:
    app.logger.setLevel(logging.INFO)


def in_get_flat(now=None):
    if now is None: now = datetime.now(CT)
    t = now.timetz() if hasattr(now, "timetz") else now
    return GET_FLAT_START <= t <= GET_FLAT_END

# ─── Auth & HTTP ───────────────────────────────────────
def authenticate():
    global _token, _token_expiry
    app.logger.info("Authenticating to Topstep API...")
    resp = requests.post(f"{PX_BASE}/api/Auth/loginKey",
        json={"userName":USER_NAME,"apiKey":API_KEY},
        headers={"Content-Type":"application/json"})
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        app.logger.error("Auth failed: %s", data)
        raise RuntimeError("Auth failed")
    _token = data["token"]
    _token_expiry = time.time() + 23*3600
    app.logger.info("Authentication successful; token expires in ~23h.")

def ensure_token():
    with lock:
        if _token is None or time.time() >= _token_expiry:
            authenticate()

def post(path, payload):
    ensure_token()
    full_url = f"{PX_BASE}{path}"
    try:
        app.logger.info(f"POST {full_url} payload={payload}")
        resp = requests.post(full_url,
            json=payload,
            headers={"Content-Type":"application/json","Authorization":f"Bearer {_token}"})
        if resp.status_code == 429:
            app.logger.warning(f"Rate limit: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        app.logger.info(f"Response: {resp.status_code} {resp.text[:200]}")
        return resp.json()
    except Exception as e:
        app.logger.error(f"HTTP error at {path} payload={payload}: {e}")
        raise

# ─── Order/Pos/Trade Helpers ──────────────────────────
def place_market(acct_id, cid, side, size):
    app.logger.info(f"Placing market order: acct_id={acct_id}, cid={cid}, side={side}, size={size}")
    resp = post("/api/Order/place", {"accountId":acct_id,"contractId":cid,"type":2,"side":side,"size":size})
    app.logger.info(f"Market order response: {resp}")
    return resp

def place_limit(acct_id, cid, side, size, px):
    app.logger.info(f"Placing limit order: acct_id={acct_id}, cid={cid}, side={side}, size={size}, px={px}")
    resp = post("/api/Order/place", {"accountId":acct_id,"contractId":cid,"type":1,"side":side,"size":size,"limitPrice":px})
    app.logger.info(f"Limit order response: {resp}")
    return resp

def place_stop(acct_id, cid, side, size, px):
    app.logger.info(f"Placing stop order: acct_id={acct_id}, cid={cid}, side={side}, size={size}, px={px}")
    resp = post("/api/Order/place", {"accountId":acct_id,"contractId":cid,"type":4,"side":side,"size":size,"stopPrice":px})
    app.logger.info(f"Stop order response: {resp}")
    return resp

def search_open(acct_id):
    app.logger.info(f"Searching open orders for acct_id={acct_id}")
    orders = post("/api/Order/searchOpen", {"accountId":acct_id}).get("orders",[])
    app.logger.info(f"Found {len(orders)} open orders.")
    return orders

def cancel(acct_id, o):
    app.logger.info(f"Cancelling order: acct_id={acct_id}, orderId={o}")
    resp = post("/api/Order/cancel", {"accountId":acct_id,"orderId":o})
    app.logger.info(f"Cancel order response: {resp}")
    return resp

def search_pos(acct_id):
    app.logger.info(f"Searching open positions for acct_id={acct_id}")
    positions = post("/api/Position/searchOpen",{"accountId":acct_id}).get("positions",[])
    app.logger.info(f"Found {len(positions)} open positions.")
    return positions

def close_pos(acct_id, c):
    app.logger.info(f"Closing position: acct_id={acct_id}, contractId={c}")
    resp = post("/api/Position/closeContract",{"accountId":acct_id,"contractId":c})
    app.logger.info(f"Close position response: {resp}")
    return resp

def search_trades(acct_id, s):
    app.logger.info(f"Searching trades for acct_id={acct_id}, since={s}")
    trades = post("/api/Trade/search", {"accountId":acct_id,"startTimestamp":s.isoformat()}).get("trades",[])
    app.logger.info(f"Found {len(trades)} trades.")
    return trades

# ─── Contract Lookup ───────────────────────────────────
def get_contract(sym):
    if OVERRIDE_CONTRACT_ID:
        app.logger.info(f"Using override contract id: {OVERRIDE_CONTRACT_ID}")
        return OVERRIDE_CONTRACT_ID
    root = re.match(r"^([A-Za-z]+)", sym).group(1)
    ctrs = post("/api/Contract/search",{"searchText":root,"live":True}).get("contracts",[])
    for c in ctrs:
        if c.get("activeContract"): 
            app.logger.info(f"Found active contract: {c['id']}")
            return c["id"]
    if ctrs: 
        app.logger.info(f"Using first found contract: {ctrs[0]['id']}")
        return ctrs[0]["id"]
    app.logger.error(f"No contract '{root}' found")
    raise ValueError(f"No contract '{root}'")

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
        app.logger.info(f"Calling n8n AI for epsilon filter: {payload}")
        resp = requests.post(N8N_AI_URL, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        dec = str(data.get('action', '')).upper()
        reason = data.get('reason', '')
        chart_url = data.get('chart_url', '')
        # Log AI's reasoning and chart for review
        log_ai_decision(account, strat, sig, sym, size, dec, reason, chart_url)
        app.logger.info(f"AI response: {data}")
        # Only approve BUY or SELL (not HOLD, etc)
        return dec in ("BUY", "SELL"), reason, chart_url
    except Exception as e:
        log_ai_decision(account, strat, sig, sym, size, "ERROR", str(e), "")
        app.logger.error(f"AI error for epsilon: {e}")
        return False, f"AI error: {str(e)}", None

def log_ai_decision(account, strat, sig, sym, size, dec, reason, chart_url):
    with open("ai_trading_log.txt", "a") as f:
        f.write(f"{datetime.now()} | {account} | {strat} | {sig} | {sym} | size={size} | {dec} | {reason} | {chart_url}\n")

# ─── Strategy: Bracket ─────────────────────────────────
def run_bracket(acct_id, sym, sig, size):
    app.logger.info(f"Running bracket strategy: acct_id={acct_id}, sym={sym}, sig={sig}, size={size}")
    cid      = get_contract(sym)
    side     = 0 if sig=="BUY" else 1
    exit_side= 1-side

    pos = [p for p in search_pos(acct_id) if p["contractId"]==cid]
    app.logger.info(f"Open positions for this contract: {pos}")

    # skip same side
    if any((side==0 and p["type"]==1) or (side==1 and p["type"]==2) for p in pos):
        app.logger.info("Skipping signal, already in same-side position.")
        return jsonify(status="ok",strategy="bracket",message="skip same"),200

    # flatten opposite (SAFE FLIP)
    if any((side==0 and p["type"]==2) or (side==1 and p["type"]==1) for p in pos):
        app.logger.info("Flattening opposite position(s)...")
        # Cancel ALL open orders for this contract (TP, SL, entry, etc)
        open_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]
        for o in open_orders:
            app.logger.info(f"Cancelling open order: {o}")
            try:
                cancel(acct_id, o["id"])
            except Exception as e:
                app.logger.error(f"Error cancelling order {o['id']}: {e}")
        # Close ALL positions for this contract
        for p in pos:
            app.logger.info(f"Closing open position: {p}")
            try:
                close_pos(acct_id, p["contractId"])
            except Exception as e:
                app.logger.error(f"Error closing position {p['contractId']}: {e}")
        # Wait for cleanup (all orders/positions gone for this contract)
        max_wait = 30
        waited = 0
        while True:
            open_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]
            positions   = [p for p in search_pos(acct_id) if p["contractId"] == cid]
            if not open_orders and not positions:
                app.logger.info("All old orders/positions cleared. Proceeding to open new bracket.")
                break
            if waited >= max_wait:
                app.logger.warning(f"Timeout waiting for old orders/positions to clear after {max_wait}s! Proceeding anyway.")
                break
            app.logger.info(f"Waiting for cleanup... ({len(open_orders)} orders, {len(positions)} positions remain)")
            time.sleep(1)
            waited += 1

    # market entry
    ent = place_market(acct_id, cid, side, size)
    oid = ent["orderId"]
    app.logger.info(f"Placed market order, orderId={oid}")

    # fill price
    trades = [t for t in search_trades(acct_id, datetime.utcnow()-timedelta(minutes=5)) if t["orderId"]==oid]
    tot = sum(t["size"] for t in trades)
    price = None
    if tot:
        price = sum(t["price"]*t["size"] for t in trades)/tot
    else:
        price = ent.get("fillPrice")
    if price is None:
        app.logger.error("Could not determine fill price from trades or entry response")
        return jsonify(status="error", message="No fill price available"), 500
    app.logger.info(f"Entry fill price: {price}")

    # initial SL
    slp = price - STOP_LOSS_POINTS if side==0 else price + STOP_LOSS_POINTS
    sl  = place_stop(acct_id, cid, exit_side, size, slp)
    sl_id = sl["orderId"]
    app.logger.info(f"Placed initial stop loss: price={slp}, orderId={sl_id}")

    # TP legs
    tp_ids=[]
    n=len(TP_POINTS)
    base=size//n; rem=size-base*n
    slices=[base]*n; slices[-1]+=rem
    for pts,amt in zip(TP_POINTS,slices):
        px= price+pts if side==0 else price-pts
        r=place_limit(acct_id, cid, exit_side, amt, px)
        tp_ids.append(r["orderId"])
        app.logger.info(f"Placed TP leg: target={px}, size={amt}, orderId={r['orderId']}")

    # watcher for TP1→SL adjust, TP2→move SL to -5, TP3→cancel
    def watcher():
        a, b, c = slices
        app.logger.info(f"Watcher started for orderId={oid}. Slices: {slices}")

        def is_open(order_id):
            try:
                res = order_id in {o["id"] for o in search_open(acct_id)}
                app.logger.debug(f"is_open({order_id}) -> {res}")
                return res
            except Exception as e:
                app.logger.error(f"[watcher] API error in is_open: {e}")
                time.sleep(5)
                return False

        def cancel_all_tps():
            try:
                open_orders = search_open(acct_id)
                for o in open_orders:
                    if o["contractId"] == cid and o["type"] == 1 and o["id"] in tp_ids:  # 1 = LIMIT ORDER
                        app.logger.info(f"Watcher cancelling TP orderId={o['id']}")
                        cancel(acct_id, o["id"])
            except Exception as e:
                app.logger.error(f"[watcher] API error in cancel_all_tps: {e}")
                time.sleep(5)

        def is_flat():
            try:
                val = not any(p for p in search_pos(acct_id) if p["contractId"] == cid)
                app.logger.debug(f"is_flat() -> {val}")
                return val
            except Exception as e:
                app.logger.error(f"[watcher] API error in is_flat: {e}")
                time.sleep(5)
                return False

        # Step 1: Wait for TP1 or SL to be hit
        app.logger.info("Watcher: waiting for TP1 or SL")
        while True:
            if is_flat():
                app.logger.info("Watcher: Position flat before TP1. Cleaning up.")
                cancel_all_tps()
                return
            if not is_open(tp_ids[0]):
                app.logger.info("Watcher: TP1 filled.")
                break
            if not is_open(sl_id):
                app.logger.info("Watcher: Initial SL filled.")
                cancel_all_tps()
                return
            time.sleep(2)

        app.logger.info(f"Watcher: Cancelling initial SL orderId={sl_id}")
        cancel(acct_id, sl_id)
        new1 = place_stop(acct_id, cid, exit_side, b + c, slp)
        st1 = new1["orderId"]
        app.logger.info(f"Watcher: Re-placed SL for {b+c} contracts at {slp}, orderId={st1}")

        # Step 2: Wait for TP2 or SL to be hit
        app.logger.info("Watcher: waiting for TP2 or SL")
        while True:
            if is_flat():
                app.logger.info("Watcher: Position flat before TP2. Cleaning up.")
                cancel_all_tps()
                return
            if not is_open(tp_ids[1]):
                app.logger.info("Watcher: TP2 filled.")
                break
            if not is_open(st1):
                app.logger.info("Watcher: SL after TP1 hit. Cleaning up.")
                cancel_all_tps()
                return
            time.sleep(2)

        app.logger.info(f"Watcher: Cancelling SL after TP1 orderId={st1}")
        cancel(acct_id, st1)
        # Move SL to entry -5 (for BUY) or entry +5 (for SELL)
        slp2 = price - 5 if side == 0 else price + 5
        new2 = place_stop(acct_id, cid, exit_side, c, slp2)
        st2 = new2["orderId"]
        app.logger.info(f"Watcher: Placed BE SL for {c} contracts at {slp2}, orderId={st2}")

        # Step 3: Wait for TP3 or SL to be hit
        app.logger.info("Watcher: waiting for TP3 or SL")
        while True:
            if is_flat():
                app.logger.info("Watcher: Position flat at end. Cleaning up.")
                cancel_all_tps()
                return
            if not is_open(tp_ids[2]):
                app.logger.info("Watcher: TP3 filled. Done.")
                break
            if not is_open(st2):
                app.logger.info("Watcher: Final SL hit. Cleaning up.")
                cancel_all_tps()
                return
            time.sleep(2)

        app.logger.info("Watcher: All done, cleaned up any stray orders.")
        cancel_all_tps()

    threading.Thread(target=watcher,daemon=True).start()
    return jsonify(status="ok",strategy="bracket",entry=ent),200


# ─── Strategy: Pivot ────────────────────────────────────
def run_pivot(acct_id, sym, sig, size):
    app.logger.info(f"Running pivot strategy: acct_id={acct_id}, sym={sym}, sig={sig}, size={size}")
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1
    exit_side = 1 - side

    # Find current net position for this contract
    pos = [p for p in search_pos(acct_id) if p["contractId"] == cid]
    net_pos = 0
    for p in pos:
        if p["type"] == 1:   # LONG
            net_pos += p["size"]
        elif p["type"] == 2: # SHORT
            net_pos -= p["size"]
    app.logger.info(f"Net position for contract {cid}: {net_pos}")

    # Target net position (+size for long, -size for short, 0 for flat)
    target = size if sig == "BUY" else -size

    # If already at target, skip
    if net_pos == target:
        app.logger.info("Already at target net position; skipping pivot trade.")
        return jsonify(status="ok", strategy="pivot", message="already at target position"), 200

    # Step 1: Cancel all stops for this contract
    for o in search_open(acct_id):
        if o["contractId"] == cid and o["type"] == 4:  # Stop order
            app.logger.info(f"Cancelling stop order: {o}")
            cancel(acct_id, o["id"])

    # Step 2: If holding the opposite, flatten and then open reverse
    trade_log = []
    if net_pos * target < 0:
        app.logger.info("Flattening opposite side, then opening new position.")
        flatten_side = 1 if net_pos > 0 else 0  # If net long, close with SELL
        trade_log.append(place_market(acct_id, cid, flatten_side, abs(net_pos)))
        trade_log.append(place_market(acct_id, cid, side, size))
    # If flat, just open new
    elif net_pos == 0:
        app.logger.info("Flat; opening new position.")
        trade_log.append(place_market(acct_id, cid, side, size))
    # If partial, adjust
    elif abs(net_pos) != abs(target):
        app.logger.info("Adjusting partial position; flatten and re-open.")
        flatten_side = 1 if net_pos > 0 else 0
        trade_log.append(place_market(acct_id, cid, flatten_side, abs(net_pos)))
        trade_log.append(place_market(acct_id, cid, side, size))

    # Step 3: Place new stop for the current position
    # Find the latest entry trade price
    trades = [t for t in search_trades(acct_id, datetime.utcnow() - timedelta(minutes=5))
              if t["contractId"] == cid]
    if trades:
        entry_price = trades[-1]["price"]
    else:
        entry_price = None

    if entry_price is not None:
        stop_price = entry_price - STOP_LOSS_POINTS if side == 0 else entry_price + STOP_LOSS_POINTS
        app.logger.info(f"Placing stop for pivot trade: price={stop_price}")
        place_stop(acct_id, cid, exit_side, size, stop_price)
    else:
        app.logger.warning("No entry price available to place stop.")

    return jsonify(status="ok", strategy="pivot", message="position set", trades=trade_log), 200

# ─── Webhook Dispatcher ─────────────────────────────────
@app.route("/webhook",methods=["POST"])
def tv_webhook():
    print("WEBHOOK FIRED")
    app.logger.info("Received webhook POST.")
    data = request.get_json()
    # Secret check
    if data.get("secret") != os.getenv("WEBHOOK_SECRET"):
        app.logger.warning("Unauthorized webhook attempt")
        return jsonify(error="unauthorized"), 403
    strat = data.get("strategy","bracket").lower()
    acct  = data.get("account", DEFAULT_ACCOUNT)
    if acct: acct = acct.lower()
    sig   = data.get("signal","").upper()
    sym   = data.get("symbol","")
    size  = int(data.get("size",1))

    app.logger.info(f"Webhook data: strat={strat}, acct={acct}, sig={sig}, sym={sym}, size={size}")

    if acct not in ACCOUNTS:
        app.logger.error(f"Unknown account '{acct}'")
        return jsonify(error=f"Unknown account '{acct}'"),400
    acct_id = ACCOUNTS[acct]

    if strat not in ("bracket","pivot"):
        app.logger.error(f"Unknown strategy '{strat}'")
        return jsonify(error=f"Unknown strategy '{strat}'"),400
    if sig not in ("BUY","SELL","FLAT"):
        app.logger.error(f"Invalid signal '{sig}'")
        return jsonify(error="invalid signal"),400

    now = datetime.now(CT)
    if in_get_flat(now) and sig!="FLAT":
        app.logger.info("In get-flat window; changing signal to FLAT.")
        sig = "FLAT"

    # AI implementation for epsilon account, using n8n vision workflow
    if acct == "epsilon":
        allow, reason, chart_url = ai_trade_decision(acct, strat, sig, sym, size)
        if not allow:
            app.logger.info(f"AI BLOCKED TRADE: {sig} {sym} size={size} reason={reason}")
            return jsonify(status="blocked", reason=reason, chart=chart_url), 200
        app.logger.info(f"AI APPROVED TRADE: {sig} {sym} size={size} reason={reason}")

    if strat=="bracket":
        return run_bracket(acct_id, sym, sig, size)
    else:
        return run_pivot(acct_id, sym, sig, size)

if __name__=="__main__":
    app.logger.info("Starting tradingview_projectx_bot server.")
    app.run(host="0.0.0.0",port=TV_PORT)
