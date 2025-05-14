#!/usr/bin/env python3
# tradingview_projectx_bot.py

import os, time, threading, requests, re
from flask import Flask, request, jsonify, g
from dotenv import load_dotenv
from datetime import datetime, timedelta, time as dtime
import pytz

# ─── Load config ───────────────────────────────────────
load_dotenv()
TV_PORT       = int(os.getenv("TV_PORT", 5000))
PX_BASE       = os.getenv("PROJECTX_BASE_URL")
USER_NAME     = os.getenv("PROJECTX_USERNAME")
API_KEY       = os.getenv("PROJECTX_API_KEY")

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
GET_FLAT_START = dtime(15,10)
GET_FLAT_END   = dtime(17,0)

app = Flask(__name__)
_token = None
_token_expiry = 0
lock = threading.Lock()

def in_get_flat(now=None):
    if now is None: now = datetime.now(CT)
    t = now.timetz() if hasattr(now, "timetz") else now
    return GET_FLAT_START <= t <= GET_FLAT_END

# ─── Auth & HTTP ───────────────────────────────────────
def authenticate():
    global _token, _token_expiry
    resp = requests.post(f"{PX_BASE}/api/Auth/loginKey",
        json={"userName":USER_NAME,"apiKey":API_KEY},
        headers={"Content-Type":"application/json"})
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError("Auth failed")
    _token = data["token"]
    _token_expiry = time.time() + 23*3600

def ensure_token():
    with lock:
        if _token is None or time.time() >= _token_expiry:
            authenticate()

def post(path, payload):
    ensure_token()
    resp = requests.post(f"{PX_BASE}{path}",
        json=payload,
        headers={"Content-Type":"application/json","Authorization":f"Bearer {_token}"})
    resp.raise_for_status()
    return resp.json()

# ─── Order/Pos/Trade Helpers ──────────────────────────
def place_market(acct_id, cid, side, size):      return post("/api/Order/place",      {"accountId":acct_id,"contractId":cid,"type":2,"side":side,"size":size})
def place_limit(acct_id, cid, side, size, px):   return post("/api/Order/place",      {"accountId":acct_id,"contractId":cid,"type":1,"side":side,"size":size,"limitPrice":px})
def place_stop(acct_id, cid, side, size, px):    return post("/api/Order/place",      {"accountId":acct_id,"contractId":cid,"type":4,"side":side,"size":size,"stopPrice":px})
def search_open(acct_id):                        return post("/api/Order/searchOpen", {"accountId":acct_id}).get("orders",[])
def cancel(acct_id, o):                          return post("/api/Order/cancel",     {"accountId":acct_id,"orderId":o})
def search_pos(acct_id):                         return post("/api/Position/searchOpen",{"accountId":acct_id}).get("positions",[])
def close_pos(acct_id, c):                       return post("/api/Position/closeContract",{"accountId":acct_id,"contractId":c})
def search_trades(acct_id, s):                   return post("/api/Trade/search",    {"accountId":acct_id,"startTimestamp":s.isoformat()}).get("trades",[])

# ─── Contract Lookup ───────────────────────────────────
def get_contract(sym):
    if OVERRIDE_CONTRACT_ID: return OVERRIDE_CONTRACT_ID
    root = re.match(r"^([A-Za-z]+)", sym).group(1)
    ctrs = post("/api/Contract/search",{"searchText":root,"live":True}).get("contracts",[])
    for c in ctrs:
        if c.get("activeContract"): return c["id"]
    if ctrs: return ctrs[0]["id"]
    raise ValueError(f"No contract '{root}'")

# ─── Strategy: Bracket ─────────────────────────────────
def run_bracket(acct_id, sym, sig, size):
    cid      = get_contract(sym)
    side     = 0 if sig=="BUY" else 1
    exit_side= 1-side

    pos = [p for p in search_pos(acct_id) if p["contractId"]==cid]

    # skip same side
    if any((side==0 and p["type"]==1) or (side==1 and p["type"]==2) for p in pos):
        return jsonify(status="ok",strategy="bracket",message="skip same"),200

    # flatten opposite
    if any((side==0 and p["type"]==2) or (side==1 and p["type"]==1) for p in pos):
        for o in search_open(acct_id): cancel(acct_id, o["id"])
        for p in pos: close_pos(acct_id, p["contractId"])

    # market entry
    ent = place_market(acct_id, cid, side, size)
    oid = ent["orderId"]

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

    # watcher for TP1→SL adjust, TP2→BE, TP3→cancel
    def watcher():
        a,b,c = slices
        # wait tp1
        while tp_ids[0] in {o["id"] for o in search_open(acct_id)}: time.sleep(5)
        cancel(acct_id, sl_id)
        new1=place_stop(acct_id, cid, exit_side, b+c, slp)
        st1=new1["orderId"]

        # wait tp2
        while tp_ids[1] in {o["id"] for o in search_open(acct_id)}: time.sleep(5)
        cancel(acct_id, st1)
        new2=place_stop(acct_id, cid, exit_side, c, price)
        st2=new2["orderId"]

        # wait tp3, then remove last SL
        while tp_ids[2] in {o["id"] for o in search_open(acct_id)}: time.sleep(5)
        cancel(acct_id, st2)

    threading.Thread(target=watcher,daemon=True).start()
    return jsonify(status="ok",strategy="bracket",entry=ent),200

# ─── Strategy: Pivot ────────────────────────────────────
def run_pivot(acct_id, sym, sig, size):
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1
    exit_side = 1 - side

    # 1. Cancel ALL existing stop orders for this contract (type 4 = stop)
    for o in search_open(acct_id):
        if o["contractId"] == cid and o.get("type", 0) == 4:
            cancel(acct_id, o["id"])

    # 2. Check net position (sum open positions: + = long, - = short)
    pos_list = [p for p in search_pos(acct_id) if p["contractId"] == cid]
    net_pos = 0
    for p in pos_list:
        if p["type"] == 1:  # long
            net_pos += p["size"]
        elif p["type"] == 2:  # short
            net_pos -= p["size"]

    # 3. If there’s an existing position, flatten and reverse
    total_trade_size = size
    if (side == 0 and net_pos < 0):  # go long, currently short
        total_trade_size = abs(net_pos) + size
    elif (side == 1 and net_pos > 0):  # go short, currently long
        total_trade_size = abs(net_pos) + size
    elif net_pos == 0:
        total_trade_size = size
    elif (side == 0 and net_pos > 0) or (side == 1 and net_pos < 0):
        # Already holding in same direction, do nothing or optionally add/scale in
        return jsonify(status="ok",strategy="pivot",message="Already in position"),200

    # Flatten old position (if any), then place new position (single market order for total_trade_size)
    if net_pos != 0 and ((side == 0 and net_pos < 0) or (side == 1 and net_pos > 0)):
        close_pos(acct_id, cid)  # ensure old is closed

    # 4. Market entry for the new position
    ent = place_market(acct_id, cid, side, total_trade_size)
    trades = [t for t in search_trades(acct_id, datetime.utcnow()-timedelta(minutes=5)) if t["orderId"] == ent["orderId"]]
    tot = sum(t["size"] for t in trades)
    price = (sum(t["price"]*t["size"] for t in trades)/tot) if tot else ent.get("fillPrice")
    if price is None:
        return jsonify(status="error",message="No fill price found"),500

    # 5. Place new stop loss for the correct size
    slp = price - STOP_LOSS_POINTS if side == 0 else price + STOP_LOSS_POINTS
    sl = place_stop(acct_id, cid, exit_side, total_trade_size, slp)

    return jsonify(status="ok",strategy="pivot",entry=ent,stop=sl),200




# ─── Webhook Dispatcher ─────────────────────────────────
@app.route("/webhook",methods=["POST"])
def tv_webhook():
    data = request.get_json()
    strat = data.get("strategy","bracket").lower()
    acct  = data.get("account", DEFAULT_ACCOUNT)
    if acct: acct = acct.lower()
    sig   = data.get("signal","").upper()
    sym   = data.get("symbol","")
    size  = int(data.get("size",1))

    if acct not in ACCOUNTS:
        return jsonify(error=f"Unknown account '{acct}'"),400
    acct_id = ACCOUNTS[acct]

    if strat not in ("bracket","pivot"):
        return jsonify(error=f"Unknown strategy '{strat}'"),400
    if sig not in ("BUY","SELL","FLAT"):
        return jsonify(error="invalid signal"),400

    # enforce get-flat window for both
    now = datetime.now(CT)
    if in_get_flat(now) and sig!="FLAT":
        sig = "FLAT"

    if strat=="bracket":
        return run_bracket(acct_id, sym, sig, size)
    else:
        return run_pivot(acct_id, sym, sig, size)

if __name__=="__main__":
    app.run(host="0.0.0.0",port=TV_PORT)
