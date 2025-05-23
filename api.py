# api.py
import requests
import logging
import json
import time
from datetime import datetime
import pytz

from auth import ensure_token, get_token
from config import load_config

session = requests.Session()
config = load_config()
OVERRIDE_CONTRACT_ID = config['OVERRIDE_CONTRACT_ID']
PX_BASE = config['PX_BASE']
SUPABASE_URL = config['SUPABASE_URL']
SUPABASE_KEY = config['SUPABASE_KEY']
CT = pytz.timezone("America/Chicago")



# ─── API Functions ────────────────────────────────────
def post(path, payload):
   logging.error("🔥 POST WRAPPER CALLED 🔥")
   

    # --- Aggressively enforce payload wrapping ---
    if not isinstance(payload, dict) or "accountId" not in payload.get("request", payload):
        # Always rebuild to be correct
        if "accountId" in payload:
            payload = {"request": payload}
        elif "request" in payload and "accountId" in payload["request"]:
            pass  # already good
        else:
            raise ValueError(f"Payload missing accountId: {payload}")
    # Now always type-cast accountId
    if "request" in payload and "accountId" in payload["request"]:
        try:
            payload["request"]["accountId"] = int(payload["request"]["accountId"])
        except Exception as e:
            logging.error(f"Failed to cast nested accountId to int: {payload['request']['accountId']} - {e}")
    # ...rest of function...


    ensure_token()
    url = f"{PX_BASE}{path}"
    logging.debug("POST %s payload=%s", url, payload)
    logging.error(f"OUTGOING PAYLOAD for {path}: {json.dumps(payload)}")

    resp = session.post(
        url,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {get_token()}"
        },
        timeout=(3.05, 10)
    )
    if resp.status_code == 429:
        logging.warning("Rate limit hit: %s %s", resp.status_code, resp.text)
    if resp.status_code >= 400:
        logging.error("Error on POST %s: %s", url, resp.text)
    resp.raise_for_status()
    data = resp.json()
    logging.debug("Response JSON: %s", data)
    return data




def place_market(acct_id, cid, side, size):
    logging.info("Placing market order acct=%s cid=%s side=%s size=%s", acct_id, cid, side, size)
    return post("/api/Order/place", {
        "accountId": acct_id, "contractId": cid,
        "type": 2, "side": side, "size": size
    })

def place_limit(acct_id, cid, side, size, px):
    logging.info("Placing limit order acct=%s cid=%s size=%s px=%s", acct_id, cid, size, px)
    return post("/api/Order/place", {
        "accountId": acct_id, "contractId": cid,
        "type": 1, "side": side, "size": size, "limitPrice": px
    })

def place_stop(acct_id, cid, side, size, px):
    logging.info("Placing stop order acct=%s cid=%s size=%s px=%s", acct_id, cid, size, px)
    return post("/api/Order/place", {
        "accountId": acct_id, "contractId": cid,
        "type": 4, "side": side, "size": size, "stopPrice": px
    })

def search_open(acct_id):
    logging.error(f"[DEBUG] search_pos called with acct_id={acct_id!r} ({type(acct_id)})")
    orders = post("/api/Order/searchOpen", {"accountId": acct_id}).get("orders", [])
    logging.debug("Open orders for %s: %s", acct_id, orders)
    return orders

def cancel(acct_id, order_id):
    resp = post("/api/Order/cancel", {"accountId": acct_id, "orderId": order_id})
    if not resp.get("success", True):
        logging.warning("Cancel reported failure: %s", resp)
    return resp

def search_pos(acct_id):
    logging.error(f"[DEBUG] search_pos called with acct_id={acct_id!r} ({type(acct_id)})")
    pos = post("/api/Position/searchOpen", {"accountId": acct_id}).get("positions", [])
    logging.debug("Open positions for %s: %s", acct_id, pos)
    return pos


def close_pos(acct_id, cid):
    resp = post("/api/Position/closeContract", {"accountId": acct_id, "contractId": cid})
    if not resp.get("success", True):
        logging.warning("Close position reported failure: %s", resp)
    return resp

def search_trades(acct_id, since):
    trades = post("/api/Trade/search", {"accountId": acct_id, "startTimestamp": since.isoformat()}).get("trades", [])
    return trades

def flatten_contract(acct_id, cid, timeout=10):
    logging.info("Flattening contract %s for acct %s", cid, acct_id)
    end = time.time() + timeout
    while time.time() < end:
        open_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]
        if not open_orders:
            break
        for o in open_orders:
            try:
                cancel(acct_id, o["id"])
            except Exception as e:
                logging.error("Error cancelling %s: %s", o["id"], e)
        time.sleep(1)
    while time.time() < end:
        positions = [p for p in search_pos(acct_id) if p["contractId"] == cid]
        if not positions:
            break
        for _ in positions:
            try:
                close_pos(acct_id, cid)
            except Exception as e:
                logging.error("Error closing position %s: %s", cid, e)
        time.sleep(1)
    while time.time() < end:
        rem_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]
        rem_pos    = [p for p in search_pos(acct_id) if p["contractId"] == cid]
        if not rem_orders and not rem_pos:
            logging.info("Flatten complete for %s", cid)
            return True
        logging.info("Waiting for flatten: %d orders, %d positions remain", len(rem_orders), len(rem_pos))
        time.sleep(1)
    logging.error("Flatten timeout: %s still has %d orders, %d positions", cid, len(rem_orders), len(rem_pos))
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




def log_trade_results_to_supabase(acct_id, cid, entry_time, ai_decision_id, meta=None):
    logging.info("Attempting to log trade results to Supabase")
    meta = meta or {}
    resp = post("/api/Trade/search", {
        "accountId": acct_id,
        "startTimestamp": entry_time.isoformat()
    })
    trades = resp.get("trades", [])
    # Only trades for this contract, not voided, nonzero size
    relevant_trades = [
        t for t in trades
        if t.get("contractId") == cid and not t.get("voided", False) and t.get("size", 0) > 0
    ]
    if not relevant_trades:
        logging.warning("No relevant trades found, skipping Supabase log.")
        return
    total_pnl = sum(t.get("profitAndLoss", 0) for t in relevant_trades)
    trade_ids = [t.get("id") for t in relevant_trades]
    exit_time = datetime.now(CT)
    payload = {
        "ai_decision_id": ai_decision_id,
        "order_id": meta.get("order_id"),
        "trade_ids": trade_ids,
        "symbol": meta.get("symbol"),
        "account": meta.get("account"),
        "strategy": meta.get("strategy"),
        "signal": meta.get("signal"),
        "entry_time": entry_time.isoformat(),
        "exit_time": exit_time.isoformat(),
        "duration_sec": int((exit_time - entry_time).total_seconds()),
        "size": meta.get("size"),
        "total_pnl": total_pnl,
        "alert": meta.get("alert"),
        "raw_trades": relevant_trades,
        "comment": meta.get("comment", ""),
    }
    url = f"{SUPABASE_URL}/rest/v1/trade_results"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }
    try:
        logging.info(f"Uploading to Supabase: {url}")
        r = session.post(url, json=payload, headers=headers, timeout=(3.05, 10))
        logging.info(f"Supabase status: {r.status_code}, {r.text}")
        r.raise_for_status()
    except Exception as e:
        logging.error(f"Supabase upload failed: {e}")
        logging.error(f"Payload that failed: {json.dumps(payload)[:1000]}")
        try:
            with open("/tmp/trade_results_fallback.jsonl", "a") as f:
                f.write(json.dumps(payload) + "\n")
            logging.info("Trade result written to local fallback log.")
        except Exception as e2:
            logging.error(f"Failed to write trade result to local log: {e2}")
