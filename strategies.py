# strategies.py

import logging
import time
import math
from datetime import datetime, timedelta

from api import (
    get_contract, search_pos, flatten_contract, place_market,
    place_limit, place_stop, search_open, cancel, search_trades,
    check_for_phantom_orders, log_trade_results_to_supabase
)
from signalr_listener import track_trade, trade_meta
from config import load_config
from market_regime import MarketRegime
from api import get_market_conditions_summary


config = load_config()
STOP_LOSS_POINTS = config.get('STOP_LOSS_POINTS', 10.0)
TP_POINTS = config.get('TP_POINTS', [2.5, 5.0, 10.0])
CT = config['CT']
ACCOUNTS = config['ACCOUNTS']
AI_ENDPOINTS = config.get('AI_ENDPOINTS', {})
LEGACY_DISABLED_MSG = "Legacy strategies are disabled under the reduction MVP."

# --- Tick handling (MES = 0.25). If you expand to other contracts later,
# consider mapping symbols to tick sizes in config.
TICK = float(config.get('TICK_SIZE', 0.25))

def round_to_tick(px: float) -> float:
    """Round a price to the nearest valid tick."""
    return round(round(px / TICK) * TICK, 2)

def round_stop_away_from_entry(entry: float, side: int, stop: float) -> float:
    """
    Round a protective stop AWAY from entry so rounding never makes it less protective.
      side: 0=BUY(long), 1=SELL(short)
    """
    if side == 0:
        # Long -> stop below entry: round down
        ticks = math.floor(stop / TICK)
    else:
        # Short -> stop above entry: round up
        ticks = math.ceil(stop / TICK)
    return round(ticks * TICK, 2)

def ensure_live_stop(acct_id: int, cid: str, exit_side: int, size: int, target_price: float,
                     entry_price: float, side: int, retries: int = 1, wait_s: float = 2.0) -> dict:
    """
    Place a stop (rounded correctly) and verify it appears in open orders.
    Retry once with re-rounded price if it doesn't.
    """
    slp = round_stop_away_from_entry(entry_price, side, target_price)
    resp = place_stop(acct_id, cid, exit_side, size, slp)

    # quick poll to confirm it exists
    for _ in range(2):
        oo = [o for o in search_open(acct_id) if o.get("contractId") == cid and o.get("type") == 4]
        if oo:
            return {"orderId": oo[0]["id"], "price": slp}
        time.sleep(wait_s)

    if retries > 0:
        logging.warning(f"Stop not visible; retrying once with rounded price @ {slp}")
        return ensure_live_stop(acct_id, cid, exit_side, size, slp, entry_price, side, retries-1, wait_s)

    logging.error("Failed to confirm protective stop is live after retry.")
    return {"orderId": resp.get("orderId"), "price": slp}


def get_regime_adjusted_params(base_sl_points: float, base_tp_points: list, regime_data: dict = None) -> tuple:
    raise RuntimeError(LEGACY_DISABLED_MSG)


def _compute_entry_fill(acct_id: int, oid: int) -> float | None:
    """Find a recent fill price for order id `oid` with a short wait/poll."""
    price = None
    for _ in range(12):
        trades = [t for t in search_trades(acct_id, datetime.now(CT) - timedelta(minutes=5)) if t.get("orderId") == oid]
        tot = sum(t.get("size", 0) for t in trades)
        if tot:
            price = sum(t["price"] * t["size"] for t in trades) / tot
            break
        # fallback to immediate response field if present
        # (some brokers echo 'fillPrice' on market orders)
        # If not present yet, sleep and poll again.
        time.sleep(0.25)
    return price


def run_bracket(acct_id, sym, sig, size, alert, ai_decision_id=None):
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1
    exit_side = 1 - side
    pos = [p for p in search_pos(acct_id) if p["contractId"] == cid]

    # skip if same-direction position already exists
    if any((side == 0 and p["type"] == 1) or (side == 1 and p["type"] == 2) for p in pos):
        return

    # flatten if opposite position exists
    if any((side == 0 and p["type"] == 2) or (side == 1 and p["type"] == 1) for p in pos):
        if not flatten_contract(acct_id, cid, timeout=10):
            return

    # Get market regime (best-effort)
    try:
        regime_data = get_market_conditions_summary()
    except Exception:
        regime_data = None

    ent = place_market(acct_id, cid, side, size)
    oid = ent.get("orderId")
    entry_time = datetime.now(CT)

    price = _compute_entry_fill(acct_id, oid) or ent.get("fillPrice")
    if price is None:
        logging.error("run_bracket: No fill price found; aborting.")
        return

    # regime-adjusted params
    adjusted_sl, adjusted_tp = get_regime_adjusted_params(STOP_LOSS_POINTS, TP_POINTS, regime_data)

    # --- Protective stop (rounded, verified)
    sl_target = price - adjusted_sl if side == 0 else price + adjusted_sl
    sl_info = ensure_live_stop(acct_id, cid, exit_side, size, sl_target, price, side)
    sl_id = sl_info.get("orderId")

    # --- Take-profits (rounded to tick; zero-size guard)
    tp_ids = []
    n = max(1, len(adjusted_tp))
    base = size // n
    rem = size - base * n
    slices = [base] * n
    if slices:
        slices[-1] += rem

    for pts, amt in zip(adjusted_tp, slices):
        if amt <= 0:
            continue
        raw_px = price + pts if side == 0 else price - pts
        px = round_to_tick(raw_px)
        r = place_limit(acct_id, cid, exit_side, amt, px)
        if r and "orderId" in r:
            tp_ids.append(r["orderId"])

    # Track with regime info
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
        trades=None,
        regime=regime_data.get('regime', 'unknown') if regime_data else 'unknown'
    )

    check_for_phantom_orders(acct_id, cid)


def run_brackmod(acct_id, sym, sig, size, alert, ai_decision_id=None):
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1
    exit_side = 1 - side
    pos = [p for p in search_pos(acct_id) if p["contractId"] == cid]

    # skip if same-direction position already exists
    if any((side == 0 and p["type"] == 1) or (side == 1 and p["type"] == 2) for p in pos):
        return

    # flatten if opposite position exists
    if any((side == 0 and p["type"] == 2) or (side == 1 and p["type"] == 1) for p in pos):
        if not flatten_contract(acct_id, cid, timeout=10):
            return

    # Get market regime (best-effort)
    try:
        regime_data = get_market_conditions_summary()
    except Exception:
        regime_data = None

    ent = place_market(acct_id, cid, side, size)
    oid = ent.get("orderId")
    entry_time = datetime.now(CT)

    price = _compute_entry_fill(acct_id, oid) or ent.get("fillPrice")
    if price is None:
        logging.error("run_brackmod: No fill price found; aborting.")
        return

    # Base brackmod params, then regime-adjust
    BASE_STOP_LOSS_POINTS = 5.75
    BASE_TP_POINTS = [2.5, 5.0]
    adjusted_sl, adjusted_tp = get_regime_adjusted_params(BASE_STOP_LOSS_POINTS, BASE_TP_POINTS, regime_data)

    # --- Protective stop (rounded, verified)
    sl_target = price - adjusted_sl if side == 0 else price + adjusted_sl
    sl_info = ensure_live_stop(acct_id, cid, exit_side, size, sl_target, price, side)
    sl_id = sl_info.get("orderId")

    # Brackmod TP sizing (supports size!=3 gracefully)
    if size == 3:
        slices = [2, 1]
    elif size >= 2:
        slices = [1, size - 1]   # partial then runner
    else:
        slices = [1]             # single-clip

    tp_ids = []
    for pts, amt in zip(adjusted_tp, slices[:len(adjusted_tp)]):
        if amt <= 0:
            continue
        raw_px = price + pts if side == 0 else price - pts
        px = round_to_tick(raw_px)
        r = place_limit(acct_id, cid, exit_side, amt, px)
        if r and "orderId" in r:
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
        trades=None,
        regime=regime_data.get('regime', 'unknown') if regime_data else 'unknown'
    )

    check_for_phantom_orders(acct_id, cid)


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

    # Generate numeric AI decision ID for manual trades (Supabase-safe)
    if ai_decision_id is None:
        ai_decision_id = int(time.time() * 1000) % (2**53)
        logging.info(f"Generated numeric AI decision ID for manual trade: {ai_decision_id}")

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

    # Clear ALL old metadata for this contract before starting new position
    if (acct_id, cid) in trade_meta:
        old_meta = trade_meta.pop((acct_id, cid))
        logging.info(f"Cleared old metadata for pivot: {old_meta.get('session_id', 'unknown')}")

    # Cancel existing stops first
    for o in search_open(acct_id):
        if o["contractId"] == cid and o["type"] == 4:
            cancel(acct_id, o["id"])
            logging.info(f"Cancelled existing stop order {o['id']} before pivot")

    # Small delay after cancelling
    if any(o["contractId"] == cid and o["type"] == 4 for o in search_open(acct_id)):
        time.sleep(0.5)

    # Position reversal logic
    if net_pos * target < 0:
        # Opposite position exists - flatten first then open new
        flatten_side = 1 if net_pos > 0 else 0
        logging.info(f"Pivot: Flattening opposite position of {abs(net_pos)} contracts")
        flatten_order = place_market(acct_id, cid, flatten_side, abs(net_pos))
        trade_log.append(flatten_order)
        time.sleep(1)  # Wait for flatten to complete

        # Then open new position
        logging.info(f"Pivot: Opening new position of {size} contracts")
        ent = place_market(acct_id, cid, side, size)
        trade_log.append(ent)
        oid = ent.get("orderId")
    elif net_pos == 0:
        # No position - just open
        logging.info(f"Pivot: Opening position of {size} contracts (was flat)")
        ent = place_market(acct_id, cid, side, size)
        trade_log.append(ent)
        oid = ent.get("orderId")
    elif abs(net_pos) != abs(target):
        # Same direction but wrong size - flatten and reopen
        flatten_side = 1 if net_pos > 0 else 0
        logging.info(f"Pivot: Adjusting position from {net_pos} to {target}")
        trade_log.append(place_market(acct_id, cid, flatten_side, abs(net_pos)))
        time.sleep(1)
        ent = place_market(acct_id, cid, side, size)
        trade_log.append(ent)
        oid = ent.get("orderId")

    # Wait a bit longer before placing stop to ensure position is established
    time.sleep(2)

    # Get fresh position data
    positions = search_pos(acct_id)
    current_pos = [p for p in positions if p["contractId"] == cid and p.get("size", 0) > 0]

    sl_id = None
    if current_pos:
        # Get recent trades to find entry price
        trades = [t for t in search_trades(acct_id, datetime.now(CT) - timedelta(minutes=5))
                  if t["contractId"] == cid and not t.get("voided", False)]

        if trades:
            entry_trades = sorted(trades, key=lambda t: t["creationTimestamp"], reverse=True)
            entry_price = None
            if oid:
                for t in entry_trades:
                    if t.get("orderId") == oid:
                        entry_price = t["price"]
                        break
            if entry_price is None and entry_trades:
                entry_price = entry_trades[0]["price"]

            if entry_price is not None:
                raw_stop = entry_price - 15.0 if side == 0 else entry_price + 15.0
                stop_price = round_stop_away_from_entry(entry_price, side, raw_stop)
                sl = place_stop(acct_id, cid, exit_side, size, stop_price)
                sl_id = sl.get("orderId")
                logging.info(f"Placed stop for pivot at {stop_price} (entry: {entry_price})")
            else:
                logging.warning("Could not determine entry price for stop")
        else:
            logging.warning("No recent trades found for stop calculation")
    else:
        logging.warning("No position found after pivot execution")

    # Track with correct session
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
