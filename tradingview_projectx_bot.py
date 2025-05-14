#!/usr/bin/env python3
# tradingview_projectx_bot.py

import os
import time
import threading
import requests
import re
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timedelta

# ─── Load config ───────────────────────────────────────
load_dotenv()
TV_PORT       = int(os.getenv("TV_PORT", 5000))
PX_BASE       = os.getenv("PROJECTX_BASE_URL")
USER_NAME     = os.getenv("PROJECTX_USERNAME")
API_KEY       = os.getenv("PROJECTX_API_KEY")

# In-memory cache for raw contract codes
CONTRACT_MAP  = {}

# Accounts will be discovered at runtime
ACCOUNTS             = {}    # name -> id
DEFAULT_ACCOUNT_NAME = None

# Bracket params
STOP_LOSS_POINTS = 10.0
TP_POINTS        = [2.5, 5.0, 10.0]

app        = Flask(__name__)
token      = None
token_expiry = 0
lock       = threading.Lock()
ACCOUNT_ID = None  # set per-request

# ─── Auth Helpers ──────────────────────────────────────
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

# ─── Account Discovery ────────────────────────────────
def list_accounts(only_active=True):
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Account/search",
        json={"onlyActiveAccounts": only_active},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json().get("accounts", [])

def build_account_map():
    """Populate ACCOUNTS and DEFAULT_ACCOUNT_NAME."""
    global ACCOUNTS, DEFAULT_ACCOUNT_NAME
    accts = list_accounts()
    ACCOUNTS = {a["name"]: a["id"] for a in accts}
    if not DEFAULT_ACCOUNT_NAME and ACCOUNTS:
        DEFAULT_ACCOUNT_NAME = next(iter(ACCOUNTS))
    app.logger.info(f"Loaded accounts: {ACCOUNTS}")
    app.logger.info(f"Default account: {DEFAULT_ACCOUNT_NAME}")

# ─── Order & Position Helpers ─────────────────────────
def place_order(payload):
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Order/place",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json()

def place_market(cid, side, size):
    return place_order({
        "accountId": ACCOUNT_ID,
        "contractId": cid,
        "type": 2,      # Market
        "side": side,
        "size": size
    })

def place_limit(cid, side, size, price):
    return place_order({
        "accountId": ACCOUNT_ID,
        "contractId": cid,
        "type": 1,      # Limit
        "side": side,
        "size": size,
        "limitPrice": price
    })

def place_stop(cid, side, size, price):
    return place_order({
        "accountId": ACCOUNT_ID,
        "contractId": cid,
        "type": 4,      # Stop
        "side": side,
        "size": size,
        "stopPrice": price
    })

def search_open_orders():
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Order/searchOpen",
        json={"accountId": ACCOUNT_ID},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json().get("orders", [])

def cancel_order(order_id):
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Order/cancel",
        json={"accountId": ACCOUNT_ID, "orderId": order_id},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json()

def search_positions():
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Position/searchOpen",
        json={"accountId": ACCOUNT_ID},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json().get("positions", [])

def close_position(cid):
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Position/closeContract",
        json={"accountId": ACCOUNT_ID, "contractId": cid},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json()

def search_trades(since: datetime):
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Trade/search",
        json={"accountId": ACCOUNT_ID, "startTimestamp": since.isoformat()},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    return resp.json().get("trades", [])

def search_contract(raw_symbol: str) -> str:
    """
    Fallback: lookup via POST /api/Contract/search using raw_symbol.
    """
    ensure_token()
    resp = requests.post(
        f"{PX_BASE}/api/Contract/search",
        json={"searchText": raw_symbol, "live": True},
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}"
        }
    )
    resp.raise_for_status()
    contracts = resp.json().get("contracts", [])
    for c in contracts:
        if c.get("activeContract"):
            return c["id"]
    if contracts:
        return contracts[0]["id"]
    raise ValueError(f"No contract found for symbol '{raw_symbol}'")

# ─── Initialization ────────────────────────────────────
initialized = False

@app.before_request
def init_once():
    global initialized
    if not initialized:
        authenticate()
        build_account_map()
        initialized = True
        app.logger.info(f"Bot initialized, default account = {DEFAULT_ACCOUNT_NAME}")

# ─── Webhook & Bracket Logic ──────────────────────────
@app.route("/webhook", methods=["POST"])
def tv_webhook():
    data = request.get_json()
    app.logger.info(f"Incoming payload: {data}")

    sig = data.get("signal", "").upper()
    if sig not in ("BUY", "SELL", "FLAT"):
        return jsonify({"error": "invalid signal"}), 400

    # pick account
    acct = data.get("account", DEFAULT_ACCOUNT_NAME)
    if acct not in ACCOUNTS:
        return jsonify({"error": f"Unknown account '{acct}'"}), 400
    global ACCOUNT_ID
    ACCOUNT_ID = ACCOUNTS[acct]

    # FLAT: cancel & close everything
    if sig == "FLAT":
        for o in search_open_orders():
            cancel_order(o["id"])
        for p in search_positions():
            close_position(p["contractId"])
        return jsonify({"status": "ok", "message": "Flattened"}), 200

    # ── Resolve contractId ───────────────────────────────
    if "contractId" in data:
        cid = int(data["contractId"])

    elif "contract" in data:
        raw = data["contract"]
        cid = CONTRACT_MAP.get(raw)
        if not cid:
            app.logger.info(f"Looking up raw contract code: {raw}")
            ensure_token()
            resp = requests.post(
                f"{PX_BASE}/api/Contract/search",
                json={"searchText": raw, "live": True},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}"
                }
            )
            resp.raise_for_status()
            contracts = resp.json().get("contracts", [])
            if not contracts:
                raise ValueError(f"No contract found for raw code '{raw}'")
            match = next((c for c in contracts if c.get("activeContract")), contracts[0])
            cid = match["id"]
            CONTRACT_MAP[raw] = cid

    # **New**: if they (mistakenly) sent your CME code in "symbol", catch that too:
    elif data.get("symbol", "").startswith("CON."):
        raw = data["symbol"]
        cid = CONTRACT_MAP.get(raw)
        if not cid:
            app.logger.info(f"Looking up raw contract code (via symbol): {raw}")
            cid = search_contract(raw)
            CONTRACT_MAP[raw] = cid

    else:
        # legacy fallback: symbol → root lookup
        cid = search_contract(data.get("symbol"))

    app.logger.info(f"Using contractId={cid}")

    # ── Place bracket ───────────────────────────────────
    size_total = int(data.get("size", 1))
    side       = 0 if sig == "BUY" else 1
    exit_side  = 1 - side

    # close opposite if exists
    positions = [p for p in search_positions() if p["contractId"] == cid]
    has_opp = any((side == 0 and p["type"] == 2) or (side == 1 and p["type"] == 1) for p in positions)
    if has_opp:
        for o in search_open_orders():
            if o["contractId"] == cid:
                cancel_order(o["id"])
        for p in positions:
            if (side == 0 and p["type"] == 2) or (side == 1 and p["type"] == 1):
                close_position(cid)

    try:
        # 1) Market entry
        entry    = place_market(cid, side, size_total)
        order_id = entry["orderId"]

        # 2) Compute weighted fill price
        since     = datetime.utcnow() - timedelta(minutes=5)
        trades    = [t for t in search_trades(since) if t["orderId"] == order_id]
        tot       = sum(t["size"] for t in trades)
        fill_pr   = (sum(t["price"] * t["size"] for t in trades) / tot) if tot else None
        if fill_pr is None:
            raise RuntimeError("No fill price from trades")

        # 3) Stop-loss
        sl_price = (fill_pr - STOP_LOSS_POINTS) if side == 0 else (fill_pr + STOP_LOSS_POINTS)
        place_stop(cid, exit_side, size_total, sl_price)

        # 4) Take-profits
        n_tp   = len(TP_POINTS)
        base   = size_total // n_tp
        rem    = size_total - base * n_tp
        slices = [base] * n_tp
        slices[-1] += rem
        for pts, sz in zip(TP_POINTS, slices):
            tp_price = (fill_pr + pts) if side == 0 else (fill_pr - pts)
            place_limit(cid, exit_side, sz, tp_price)

        return jsonify({"status": "ok", "entry": entry}), 200

    except Exception as e:
        app.logger.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

# ─── Launch ─────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=TV_PORT)
