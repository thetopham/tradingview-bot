# strategies.py

import time
from datetime import datetime, timedelta

from api import (
    get_contract, search_pos, flatten_contract, place_market,
    place_limit, place_stop, search_open, cancel, search_trades,
    check_for_phantom_orders, log_trade_results_to_supabase
)
from signalr_listener import track_trade
from config import load_config

config = load_config()
STOP_LOSS_POINTS = config.get('STOP_LOSS_POINTS', 10.0)
TP_POINTS = config.get('TP_POINTS', [2.5, 5.0, 10.0])
CT = config['CT']

def run_simple(acct_id: int, sym: str, sig: str, size: int, alert: str, ai_decision_id=None):
    """Execute a simple market order; server-side brackets handled by broker."""
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1

    positions = [p for p in search_pos(acct_id) if p.get("contractId") == cid]

    # Skip if a position already exists in the same direction
    if any((side == 0 and p.get("type") == 1) or (side == 1 and p.get("type") == 2) for p in positions):
        logging.info("run_simple: position already open in same direction; skipping entry")
        return

    # Flatten opposing exposure before sending a new order
    if any((side == 0 and p.get("type") == 2) or (side == 1 and p.get("type") == 1) for p in positions):
        if not flatten_contract(acct_id, cid, timeout=10):
            logging.error("run_simple: unable to flatten opposing position; aborting entry")
            return

    entry = place_market(acct_id, cid, side, size)
    fill_price = _compute_entry_fill(acct_id, entry.get("orderId")) or entry.get("fillPrice")

    logging.info(
        "run_simple: %s %s size=%s price=%s alert=%s decision_id=%s",
        sig,
        sym,
        size,
        fill_price,
        alert,
        ai_decision_id,
    )

def run_bracket(acct_id, sym, sig, size, alert, ai_decision_id=None):
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1
    exit_side = 1 - side
    pos = [p for p in search_pos(acct_id) if p["contractId"] == cid]

    if any((side == 0 and p["type"] == 1) or (side == 1 and p["type"] == 2) for p in pos):
        return  # skip same

    if any((side == 0 and p["type"] == 2) or (side == 1 and p["type"] == 1) for p in pos):
        success = flatten_contract(acct_id, cid, timeout=10)
        if not success:
            return  # Could not flatten contract

    ent = place_market(acct_id, cid, side, size)
    oid = ent["orderId"]
    entry_time = datetime.now(CT)

    price = None
    for _ in range(12):
        trades = [t for t in search_trades(acct_id, datetime.now(CT)-timedelta(minutes=5)) if t["orderId"]==oid]
        tot = sum(t["size"] for t in trades)
        if tot:
            price = sum(t["price"]*t["size"] for t in trades)/tot
            break
        price = ent.get("fillPrice")
        if price is not None:
            break
        time.sleep(1)

    if price is None:
        return  # No fill price

    slp = price - STOP_LOSS_POINTS if side==0 else price + STOP_LOSS_POINTS
    sl  = place_stop(acct_id, cid, exit_side, size, slp)
    sl_id = sl["orderId"]

    tp_ids = []
    n = len(TP_POINTS)
    base = size // n
    rem = size - base * n
    slices = [base] * n
    slices[-1] += rem
    for pts, amt in zip(TP_POINTS, slices):
        px = price + pts if side == 0 else price - pts
        r = place_limit(acct_id, cid, exit_side, amt, px)
        tp_ids.append(r["orderId"])

    track_trade(
        acct_id=acct_id,
        cid=cid,
        entry_time=entry_time.timestamp(),
        ai_decision_id=ai_decision_id,
        strategy="bracket",
        sig=sig,
        size=size,
        order_id=oid,
        alert=alert,
        account=acct_id,
        symbol=sym,
        sl_id=sl_id,
        tp_ids=tp_ids,
        trades=None
    )

    check_for_phantom_orders(acct_id, cid)
    # No HTTP return; just end

def run_brackmod(acct_id, sym, sig, size, alert, ai_decision_id=None):
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1
    exit_side = 1 - side
    pos = [p for p in search_pos(acct_id) if p["contractId"] == cid]
    if any((side == 0 and p["type"] == 1) or (side == 1 and p["type"] == 2) for p in pos):
        return  # skip same
    if any((side == 0 and p["type"] == 2) or (side == 1 and p["type"] == 1) for p in pos):
        success = flatten_contract(acct_id, cid, timeout=10)
        if not success:
            return  # Could not flatten contract

    ent = place_market(acct_id, cid, side, size)
    oid = ent["orderId"]
    entry_time = datetime.now(CT)
    price = None
    for _ in range(12):
        trades = [t for t in search_trades(acct_id, datetime.now(CT) - timedelta(minutes=5)) if t["orderId"] == oid]
        tot = sum(t["size"] for t in trades)
        if tot:
            price = sum(t["price"] * t["size"] for t in trades) / tot
            break
        price = ent.get("fillPrice")
        if price is not None:
            break
        time.sleep(1)
    if price is None:
        return  # No fill price

    STOP_LOSS_POINTS = 5.75
    slp = price - STOP_LOSS_POINTS if side == 0 else price + STOP_LOSS_POINTS
    sl = place_stop(acct_id, cid, exit_side, size, slp)
    sl_id = sl["orderId"]
    TP_POINTS = [2.5, 5.0]
    slices = [2, 1]
    tp_ids = []
    for pts, amt in zip(TP_POINTS, slices):
        px = price + pts if side == 0 else price - pts
        r = place_limit(acct_id, cid, exit_side, amt, px)
        tp_ids.append(r["orderId"])

    track_trade(
        acct_id=acct_id,
        cid=cid,
        entry_time=entry_time.timestamp(),
        ai_decision_id=ai_decision_id,
        strategy="brackmod",
        sig=sig,
        size=size,
        order_id=oid,
        alert=alert,
        account=acct_id,
        symbol=sym,
        sl_id=sl_id,
        tp_ids=tp_ids,
        trades=None
    )

    check_for_phantom_orders(acct_id, cid)
    # No HTTP return; just end

def run_pivot(acct_id, sym, sig, size, alert, ai_decision_id=None):
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1
    exit_side = 1 - side
    pos = [p for p in search_pos(acct_id) if p["contractId"] == cid]
    net_pos = sum(p["size"] if p["type"] == 1 else -p["size"] for p in pos)
    target = size if sig == "BUY" else -size
    entry_time = datetime.now(CT)
    trade_log = []
    oid = None

    if net_pos == target:
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
        return  # already at target position

    for o in search_open(acct_id):
        if o["contractId"] == cid and o["type"] == 4:
            cancel(acct_id, o["id"])

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

    trades = [t for t in search_trades(acct_id, datetime.now(CT) - timedelta(minutes=5))
              if t["contractId"] == cid]
    entry_price = trades[-1]["price"] if trades else None
    if entry_price is not None:
        stop_price = entry_price - STOP_LOSS_POINTS if side == 0 else entry_price + STOP_LOSS_POINTS
        sl = place_stop(acct_id, cid, exit_side, size, stop_price)
        sl_id = sl["orderId"]
    else:
        sl_id = None

    track_trade(
        acct_id=acct_id,
        cid=cid,
        entry_time=entry_time.timestamp(),
        ai_decision_id=ai_decision_id,
        strategy="pivot",
        sig=sig,
        size=size,
        order_id=oid,
        alert=alert,
        account=acct_id,
        symbol=sym,
        sl_id=sl_id,
        trades=trade_log
    )

    check_for_phantom_orders(acct_id, cid)
    # No HTTP return; just end
