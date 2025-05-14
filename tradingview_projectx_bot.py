#!/usr/bin/env python3
# tradingview_projectx_bot.py

import os, time, threading, requests, re
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timedelta, time as dtime
import pytz

# ─── Load config ───────────────────────────────────────
load_dotenv()
TV_PORT         = int(os.getenv("TV_PORT", 5000))
PX_BASE         = os.getenv("PROJECTX_BASE_URL")
USER_NAME       = os.getenv("PROJECTX_USERNAME")
API_KEY         = os.getenv("PROJECTX_API_KEY")
DEFAULT_ACCOUNT = os.getenv("DEFAULT_ACCOUNT_NAME")

# Bracket params
STOP_LOSS_POINTS = 10.0
TP_POINTS        = [2.5, 5.0, 10.0]

# Hard-coded MES contract
OVERRIDE_CONTRACT_ID = "CON.F.US.MES.M25"

# CT & get-flat window
CT             = pytz.timezone("America/Chicago")
GET_FLAT_START = dtime(15,10)
GET_FLAT_END   = dtime(17, 0)

app        = Flask(__name__)
token      = None
token_expiry=0
lock       = threading.Lock()
ACCOUNTS   = {}
ACCOUNT_ID = None

# track watcher threads
_watchers = {}

def in_get_flat_zone(now=None):
    if now is None:
        now = datetime.now(CT)
    t = now.timetz() if isinstance(now, datetime) else now
    return GET_FLAT_START <= t <= GET_FLAT_END

# ─── HTTP helpers ──────────────────────────────────────
def authenticate():
    global token, token_expiry
    r = requests.post(f"{PX_BASE}/api/Auth/loginKey",
        json={"userName":USER_NAME,"apiKey":API_KEY},
        headers={"Content-Type":"application/json"})
    r.raise_for_status(); d=r.json()
    if not d.get("success"):
        raise RuntimeError("Auth failed")
    token=d["token"]; token_expiry=time.time()+23*3600

def ensure_token():
    with lock:
        if token is None or time.time()>=token_expiry:
            authenticate()

def post(path,payload):
    ensure_token()
    r = requests.post(f"{PX_BASE}{path}",json=payload,
         headers={"Content-Type":"application/json","Authorization":f"Bearer {token}"})
    r.raise_for_status(); return r.json()

# ─── Account load ──────────────────────────────────────
@app.before_first_request
def load_accounts():
    global ACCOUNTS
    data=post("/api/Account/search",{"onlyActiveAccounts":True})
    ACCOUNTS={a["name"]:a["id"] for a in data.get("accounts",[])}
    app.logger.info(f"Accounts: {ACCOUNTS}")

# ─── API wrappers ──────────────────────────────────────
def place_market(cid,side,size):    return post("/api/Order/place",{"accountId":ACCOUNT_ID,"contractId":cid,"type":2,"side":side,"size":size})
def place_limit(cid,side,size,p):   return post("/api/Order/place",{"accountId":ACCOUNT_ID,"contractId":cid,"type":1,"side":side,"size":size,"limitPrice":p})
def place_stop(cid,side,size,p):    return post("/api/Order/place",{"accountId":ACCOUNT_ID,"contractId":cid,"type":4,"side":side,"size":size,"stopPrice":p})
def search_open_orders():           return post("/api/Order/searchOpen",{"accountId":ACCOUNT_ID}).get("orders",[])
def cancel_order(oid):              return post("/api/Order/cancel",{"accountId":ACCOUNT_ID,"orderId":oid})
def search_positions():             return post("/api/Position/searchOpen",{"accountId":ACCOUNT_ID}).get("positions",[])
def close_position(cid):            return post("/api/Position/closeContract",{"accountId":ACCOUNT_ID,"contractId":cid})
def search_trades(since):           return post("/api/Trade/search",{"accountId":ACCOUNT_ID,"startTimestamp":since.isoformat()}).get("trades",[])

# ─── Contract lookup ───────────────────────────────────
def _lookup_raw(raw):
    ctrs=post("/api/Contract/search",{"searchText":raw,"live":True}).get("contracts",[])
    for c in ctrs:
        if c.get("activeContract"): return c["id"]
    if ctrs: return ctrs[0]["id"]
    raise ValueError(f"No contract {raw}")

def search_contract(sym):
    if OVERRIDE_CONTRACT_ID:
        return OVERRIDE_CONTRACT_ID
    if sym.upper().startswith("CON."):
        return _lookup_raw(sym)
    root=re.match(r"^([A-Za-z]+)",sym).group(1)
    return _lookup_raw(root)

# ─── Watchers ──────────────────────────────────────────
def watch_tp1_resize_sl(cid, exit_side, slice1, sl_price, tp1_id):
    """When TP1 fills, resize SL to remaining size at original sl_price."""
    while True:
        time.sleep(5)
        open_ids={o["id"] for o in search_open_orders()}
        if tp1_id not in open_ids:
            # cancel old
            # note: we assume only one SL exists
            for o in search_open_orders():
                if o["type"]==4:  # stop
                    cancel_order(o["id"])
            # place new stop at same price but smaller size
            rem_size = total_size - slice1
            new = place_stop(cid, exit_side, rem_size, sl_price)
            app.logger.info(f"TP1 hit → SL resized to {rem_size} @ {sl_price}, new SL {new['orderId']}")
            return

# reuse the TP2 and TP3 watchers from earlier (unchanged)...

def watch_tp2_break_even(cid, exit_side, total_size, entry_price, sl_id, tp2_id):
    while True:
        time.sleep(5)
        open_ids={o["id"] for o in search_open_orders()}
        if tp2_id not in open_ids:
            if sl_id in open_ids: cancel_order(sl_id)
            new=place_stop(cid, exit_side, total_size, entry_price)
            app.logger.info(f"TP2 hit → SL moved to BE on {total_size}, new stop {new['orderId']}")
            return

def watch_tp3_cancel_sl(cid, exit_side, sl_id, tp3_id):
    while True:
        time.sleep(5)
        open_ids={o["id"] for o in search_open_orders()}
        if tp3_id not in open_ids:
            if sl_id in open_ids:
                cancel_order(sl_id)
                app.logger.info(f"TP3 hit → cancelled SL {sl_id}")
            return

# ─── Webhook ───────────────────────────────────────────
@app.route("/webhook",methods=["POST"])
def tv_webhook():
    data=request.get_json()
    sig=data.get("signal","").upper()
    sym=data.get("symbol","")
    size=int(data.get("size",1))
    acct=data.get("account") or DEFAULT_ACCOUNT
    now=datetime.now(CT)

    if sig not in ("BUY","SELL","FLAT"):
        return jsonify(error="invalid signal"),400

    if acct:
        if acct not in ACCOUNTS:
            return jsonify(error=f"Unknown account '{acct}'"),400
        global ACCOUNT_ID
        ACCOUNT_ID=ACCOUNTS[acct]

    if sig=="FLAT" or in_get_flat_zone(now):
        for o in search_open_orders(): cancel_order(o["id"])
        for p in search_positions():    close_position(p["contractId"])
        return jsonify(status="ok",message="flattened"),200

    side=0 if sig=="BUY" else 1
    exit_side=1-side
    cid=search_contract(sym)

    pos=[p for p in search_positions() if p["contractId"]==cid]
    if any((side==0 and p["type"]==1) or (side==1 and p["type"]==2) for p in pos):
        return jsonify(status="ok",message="already same-side"),200
    if any((side==0 and p["type"]==2) or (side==1 and p["type"]==1) for p in pos):
        for o in search_open_orders(): cancel_order(o["id"])
        for p in pos: close_position(p["contractId"])

    ent=place_market(cid,side,size)
    oid=ent["orderId"]

    trades=[t for t in search_trades(datetime.utcnow()-timedelta(minutes=5)) if t["orderId"]==oid]
    tot=sum(t["size"] for t in trades)
    price=(sum(t["price"]*t["size"] for t in trades)/tot) if tot else ent.get("fillPrice")

    # initial SL
    sl_price=price-STOP_LOSS_POINTS if side==0 else price+STOP_LOSS_POINTS
    sl_resp=place_stop(cid,exit_side,size,sl_price)
    sl_id=sl_resp["orderId"]

    # TPs
    tp_ids=[]
    global total_size, slice1
    total_size=size
    n_tp=len(TP_POINTS)
    base=size//n_tp; rem=size-base*n_tp
    slices=[base]*n_tp; slices[-1]+=rem
    slice1=slices[0]

    for pts,amt in zip(TP_POINTS,slices):
        tp_px=(price+pts) if side==0 else (price-pts)
        r=place_limit(cid,exit_side,amt,tp_px)
        tp_ids.append(r["orderId"])

    # launch TP1 watcher
    if tp_ids and tp_ids[0] not in _watchers:
        t1=tp_ids[0]
        th1=threading.Thread(target=watch_tp1_resize_sl,
            args=(cid,exit_side,slice1,sl_price,t1),daemon=True)
        th1.start(); _watchers[t1]=th1

    # TP2 & TP3 watchers unchanged...
    if len(tp_ids)>1 and tp_ids[1] not in _watchers:
        t2=tp_ids[1]
        th2=threading.Thread(target=watch_tp2_break_even,
            args=(cid,exit_side,size,price,sl_id,t2),daemon=True)
        th2.start(); _watchers[t2]=th2
    if len(tp_ids)>2 and tp_ids[2] not in _watchers:
        t3=tp_ids[2]
        th3=threading.Thread(target=watch_tp3_cancel_sl,
            args=(cid,exit_side,sl_id,t3),daemon=True)
        th3.start(); _watchers[t3]=th3

    return jsonify(status="ok",entry=ent),200

if __name__=="__main__":
    app.run(host="0.0.0.0",port=TV_PORT)
