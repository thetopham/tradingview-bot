import logging
import time
from datetime import datetime, timedelta

from api import (
    get_contract,
    search_pos,
    flatten_contract,
    place_market,
    search_trades,
)
from signalr_listener import track_trade
from config import load_config

config = load_config()
CT = config['CT']


def _compute_entry_fill(acct_id: int, oid: int) -> float | None:
    """Find a recent fill price for order id `oid` with a short wait/poll."""
    price = None
    for _ in range(12):
        trades = [t for t in search_trades(acct_id, datetime.now(CT) - timedelta(minutes=5)) if t.get("orderId") == oid]
        tot = sum(t.get("size", 0) for t in trades)
        if tot:
            price = sum(t["price"] * t["size"] for t in trades) / tot
            break
        time.sleep(0.25)
    return price


def run_simple(acct_id: int, sym: str, sig: str, size: int, alert: str, ai_decision_id=None):
    """Execute a simple market order; Topstep handles server-side brackets."""
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1

    positions = [p for p in search_pos(acct_id) if p["contractId"] == cid]

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
        "run_simple: %s %s size=%s price=%s alert=%s",
        sig, sym, size, fill_price, alert,
    )

    track_trade(
        acct_id=acct_id,
        cid=cid,
        entry_time=datetime.now(CT).timestamp(),
        ai_decision_id=ai_decision_id,
        strategy="simple",
        sig=sig,
        size=size,
        order_id=entry.get("orderId"),
        alert=alert,
        account=acct_id,
        symbol=sym,
        trades=None,
    )
