#!/usr/bin/env python3
# tradingview_projectx_bot.py

import os, time, threading, requests, re
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timedelta
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

# Hard-coded contract override
OVERRIDE_CONTRACT_ID = "CON.F.US.MES.M25"

# Single-signal cache
CONTRACT_CACHE = {}

# Timezone for CT trading-hours logic
CT = pytz.timezone("America/Chicago")
GET_FLAT_START = (15, 10)  # 3:10pm
GET_FLAT_END   = (17, 0)   #  5:00pm

app = Flask(__name__)
token = None
token_expiry = 0
lock = threading.Lock()
ACCOUNT_ID = None

def in_get_flat_zone(now=None):
    now = now or datetime.now(CT).time()
    start = datetime.now(CT).replace(hour=GET_FLAT_START[0], minute=GET_FLAT_START[1], second=0).time()
    end   = datetime.now(CT).replace(hour=GET_FLAT_END[0],   minute=GET_FLAT_END[1],   second=0).time()
    return start <= now <= end

# ─── Auth Helpers ──────────────────────────────────────
def authenticate():
    global token, token_expiry
    resp = requests.post(f"{PX_BASE}/api/Auth/loginKey",
                         json={"userName":USER_NAME,"apiKey":API_KEY},
                         headers={"Content-Type":"application/json"})
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Auth failed: {data}")
    token = data["token"]
    token_expiry = time.time() + 23*3600

def ensure_token():
    with lock:
        if token is None or time.time() > token_expiry:
            authenticate()

# ─── Low-level API calls ────────────────────────────────
def post(endpoint, payload):
    ensure_token()
    resp = requests.post(f"{PX_BASE}{endpoint}",
                         json=payload,
                         headers={"Content-Type":"application/json",
                                  "Authorization":f"Bearer {token}"})
    resp.raise_for_status()
    return resp.json()

def place_market(cid,side,size):    return post("/api/Order/place", {"accountId":ACCOUNT_ID,"contractId":cid,"type":2,"side":side,"size":size})
def place_limit(cid,side,size,price): return post("/api/Order/place", {"accountId":ACCOUNT_ID,"contractId":cid,"type":1,"side":side,"size":size,"limitPrice":price})
def place_stop(cid,side,size,price):  return post("/api/Order/place", {"accountId":ACCOUNT_ID,"contractId":cid,"type":4,"side":side,"size":size,"stopPrice":price})

def search_open_orders():
    return post("/api/Order/searchOpen",{"accountId":ACCOUNT_ID}).get("orders",[])

def cancel_order(oid):
    return post("/api/Order/cancel",{"accountId":ACCOUNT_ID,"orderId":oid})

def search_positions():
    return post("/api/Position/searchOpen",{"accountId":ACCOUNT_ID}).get("positions",[])

def close_position(cid):
    return post("/api/Position/closeContract",{"accountId":ACCOUNT_ID,"contractId":cid})

def search_trades(since):
    return post("/api/Trade/search",{"accountId":ACCOUNT_ID,"startTimestamp":since.isoformat()}).get("trades",[])

# ─── Contract Lookup ───────────────────────────────────
def _lookup_raw(raw):
    data = post("/api/Contract/search",{"searchText":raw,"live":True}).get("contracts",[])
    for c in data:
        if c.get("activeContract"): return c["id"]
    if data: return data[0]["id"]
    raise ValueError(f"No contract for code '{raw}'")

def search_contract(tv_symbol):
    # 1) override
    if OVERRIDE_CONTRACT_ID:
        return OVERRIDE_CONTRACT_ID

    # 2) raw-style
    if tv_symbol.upper().startswith("CON."):
        return CONTRACT_CACHE.setdefault(tv_symbol,_lookup_raw(tv_symbol))

    # 3) root-search
    root = re.match(r"^([A-Za-z]+)",tv_symbol).group(1)
    return _lookup_raw(root)

# ─── Init boilerplate ───────────────────────────────────
@app.before_request
def once():
    global ACCOUNT_ID
    if not token:
        authenticate()
    # pick default account from .env or first live account
    if ACCOUNT_ID is None:
        accts = post("/api/Account/search",{"onlyActiveAccounts":True}).get("accounts",[])
        ACCOUNT_ID = int(os.getenv("PROJECTX_ACCOUNT_ID") or accts[0]["id"])
        app.logger.info(f"Using Account ID {ACCOUNT_ID}")

# ─── Webhook endpoint ───────────────────────────────────
@app.route("/webhook",methods=["POST"])
def tv_webhook():
    body = request.get_json()
    sig  = body.get("signal","").upper()
    raw  = body.get("symbol","")
    size = int(body.get("size",1))
    now  = datetime.now(CT)

    # only valid signals
    if sig not in ("BUY","SELL","FLAT"):
        return jsonify(error="invalid signal"),400

    # FLAT always flattens
    if sig=="FLAT" or in_get_flat_zone(now):
        for o in search_open_orders(): cancel_order(o["id"])
        for p in search_positions():    close_position(p["contractId"])
        return jsonify(status="ok",message="flattened"),200

    # one side at a time: skip duplicates
    side     = 0 if sig=="BUY" else 1
    cid      = search_contract(raw)
    positions= [p for p in search_positions() if p["contractId"]==cid]
    if any((side==0 and p["type"]==1) or (side==1 and p["type"]==2) for p in positions):
        return jsonify(status="ok",message="already in same direction"),200

    # opposing? flatten first
    if any((side==0 and p["type"]==2) or (side==1 and p["type"]==1) for p in positions):
        for o in search_open_orders(): cancel_order(o["id"])
        for p in positions: close_position(p["contractId"])

    # place market entry
    entry = place_market(cid,side,size)
    oid   = entry["orderId"]
    # compute fill
    trades = [t for t in search_trades(datetime.utcnow()-timedelta(minutes=5)) if t["orderId"]==oid]
    tot    = sum(t["size"] for t in trades)
    price  = sum(t["price"]*t["size"] for t in trades)/tot if tot else entry.get("fillPrice")
    # stop
    sl = price - STOP_LOSS_POINTS if side==0 else price + STOP_LOSS_POINTS
    place_stop(cid,1-side,size,sl)
    # take-profits
    slices = []
    base   = size//len(TP_POINTS)
    for i in range(len(TP_POINTS)-1):
        slices.append(base)
    slices.append(size - base*(len(TP_POINTS)-1))
    for pts,sz in zip(TP_POINTS,slices):
        tp = price+pts if side==0 else price-pts
        place_limit(cid,1-side,sz,tp)

    return jsonify(status="ok",entry=entry),200

# ─── Run ────────────────────────────────────────────────
if __name__=="__main__":
    app.run(host="0.0.0.0",port=TV_PORT)
