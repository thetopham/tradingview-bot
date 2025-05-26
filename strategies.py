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
    
    # Improved dynamic slice calculation
    if size >= n:
        # If size >= number of TPs, distribute evenly with remainder to last
        base = size // n
        rem = size - base * n
        slices = [base] * n
        if rem > 0:
            # Distribute remainder more evenly, starting from last
            for i in range(rem):
                slices[-(i+1)] += 1
    else:
        # If size < number of TPs, prioritize closer TPs
        slices = [0] * n
        for i in range(size):
            slices[i] = 1
    
    # Place TP orders only for non-zero amounts
    for pts, amt in zip(TP_POINTS, slices):
        if amt > 0:
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
    
    # Dynamic slice calculation based on actual size
    n = len(TP_POINTS)
    if size >= n:
        # If size >= number of TPs, distribute evenly with remainder to last
        base = size // n
        rem = size - base * n
        slices = [base] * n
        if rem > 0:
            # Distribute remainder more evenly, starting from last
            for i in range(rem):
                slices[-(i+1)] += 1
    else:
        # If size < number of TPs, use 1 contract per TP up to size
        slices = [0] * n
        for i in range(size):
            slices[i] = 1
    
    tp_ids = []
    for pts, amt in zip(TP_POINTS, slices):
        if amt > 0:  # Only place TP orders for non-zero amounts
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
