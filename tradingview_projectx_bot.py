#!/usr/bin/env python3
# tradingview_projectx_bot.py

import os, time, threading, requests, re
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

# Hard‐coded contract
OVERRIDE_CONTRACT_ID = "CON.F.US.MES.M25"

# Timezone & get-flat window
CT = pytz.timezone("America/Chicago")
GET_FLAT_START = dtime(15,09)  # 3:09 PM CT
GET_FLAT_END   = dtime(17, 0)  # 5:00 PM CT

app = Flask(__name__)
token = None
token_expiry = 0
lock = threading.Lock()
ACCOUNT_ID = None

def in_get_flat_zone(now=None):
    """Return True if `now` (datetime or time) falls between 15:10 and 17:00 CT."""
    if now is None:
        now = datetime.now(CT)
    if isinstance(now, datetime):
        now = now.timetz()
    return GET_FLAT_START <= now <= GET_FLAT_END

# ─── Auth Helpers ──────────────────────────────────────
def authenticate():
    global token, token_expiry
    resp = requests.post(
        f"{PX_BASE}/api/Auth/loginKey",
        json={"userName": USER_NAME, "apiKey": API_KEY},
        headers={"Content-Type":"application/json"}
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

# ─── Low-level calls ───────────────────────────────────
def post(endpoint, payload):
    ensure_token()
    resp = requests.post(f"{PX_BASE}{endpoint}",
                         json=payload,
                         headers={
                             "Content-Type":"application/json",
                             "Authorization":f"Bearer {token}"
                         })
    resp.raise_for_status()
    return resp.json()

def place_market(cid,side,size):    return post("/api/Order/place",{"accountId":ACCOUNT_ID,"contractId":cid,"type":2,"side":side,"size":size})
def place_limit(cid,side,size,price): return post("/api/Order/place",{"accountId":ACCOUNT_ID,"contractId":cid,"type":1,"side":side,"size":size,"limitPrice":price})
def place_stop(cid,side,size,price):  return post("/api/Order/place",{"accountId":ACCOUNT_ID,"contractId":cid,"type":4,"side":side,"size":size,"stopPrice":price})

def search_open_orders():    return post("/api/Order/searchOpen",{"accountId":ACCOUNT_ID}).get("orders",[])
def cancel_order(oid):       return post("/api/Order/cancel",{"accountId":ACCOUNT_ID,"orderId":oid})
def search_positions():      return post("/api/Position/searchOpen",{"accountId":ACCOUNT_ID}).get("positions",[])
def close_position(cid):     return post("/api/Position/closeContract",{"accountId":ACCOUNT_ID,"contractId":cid})
def search_trades(since):    return post("/api/Trade/search",{"accountId":ACCOUNT_ID,"startTimestamp":since.isoformat()}).get("trades",[])

# ─── Contract lookup ───────────────────────────────────
def _lookup_raw(raw):
    ctrs = post("/api/Contract/search",{"searchText":raw,"live":True}).get("contracts",[])
    for c in ctrs:
        if c.get("activeContract"): return c["id"]
    if ctrs: return ctrs[0]["id"]
    raise ValueError(f"No contract for code '{raw}'")

def search_contract(sym):
    if OVERRIDE_CONTRACT_ID:
        return OVERRIDE_CONTRACT_ID
    if sym.upper().startswith("CON."):
        return _lookup_raw(sym)
    root = re.match(r"^([A-Za-z]+)", sym).group(1)
    return _lookup_raw(root)

# ─── Init ──────────────────────────────────────────────
@app.before_request
def pick_account():
    global ACCOUNT_ID
    if token is None:
        authenticate()
    if ACCOUNT_ID is None:
        # first active account or from .env
        accts = post("/api/Account/search",{"onlyActiveAccounts":True}).get("accounts",[])
        ACCOUNT_ID = int(os.getenv("PROJECTX_ACCOUNT_ID") or accts[0]["id"])
        app.logger.info(f"Using Account {ACCOUNT_ID}")

# ─── Webhook endpoint ───────────────────────────────────
@app.route("/webhook", methods=["POST"])
def tv_webhook():
    data = request.get_json()
    sig  = data.get("signal","").upper()
    sym  = data.get("symbol","")
    size = int(data.get("size",1))
    now  = datetime.now(CT)

    if sig not in ("BUY","SELL","FLAT"):
        return jsonify(error="invalid signal"),400

    # FLAT or in get-flat window
    if sig=="FLAT" or in_get_flat_zone(now):
        for o in search_open_orders(): cancel_order(o["id"])
        for p in search_positions():    close_position(p["contractId"])
        return jsonify(status="ok",message="flattened"),200

    side = 0 if sig=="BUY" else 1
    cid  = search_contract(sym)

    # skip if already same-side open
    pos = [p for p in search_positions() if p["contractId"]==cid]
    if any((side==0 and p["type"]==1) or (side==1 and p["type"]==2) for p in pos):
        return jsonify(status="ok",message="already open same side"),200

    # opposing? flatten first
    if any((side==0 and p["type"]==2) or (side==1 and p["type"]==1) for p in pos):
        for o in search_open_orders(): cancel_order(o["id"])
        for p in pos: close_position(p["contractId"])

    # 1) entry
    ent = place_market(cid,side,size)
    oid = ent["orderId"]

    # 2) fill price
    trades = [t for t in search_trades(datetime.utcnow()-timedelta(minutes=5)) if t["orderId"]==oid]
    tot    = sum(t["size"] for t in trades)
    price  = sum(t["price"]*t["size"] for t in trades)/tot if tot else ent.get("fillPrice")

    # 3) stop‐loss
    sl = price - STOP_LOSS_POINTS if side==0 else price + STOP_LOSS_POINTS
    place_stop(cid,1-side,size,sl)

    # 4) take‐profits
    n_tp = len(TP_POINTS)
    base = size//n_tp
    rem  = size - base*n_tp
    slices = [base]*n_tp
    slices[-1]+= rem
    for pts,sz in zip(TP_POINTS,slices):
        tp = price+pts if side==0 else price-pts
        place_limit(cid,1-side,sz,tp)

    return jsonify(status="ok",entry=ent),200

# ─── Boot ───────────────────────────────────────────────
if __name__=="__main__":
    app.run(host="0.0.0.0",port=TV_PORT)
