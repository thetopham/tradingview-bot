"""One-off helper to backfill entry/exit prices on existing trade_results rows."""
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from api import get_supabase_client


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backfill_trade_prices")


def _signed_qty(trade: Dict[str, Any]) -> float:
    try:
        qty = float(trade.get("size") or 0)
    except Exception:
        return 0.0
    if qty <= 0:
        return 0.0
    side = trade.get("side")
    if side == 0:
        return qty
    if side == 1:
        return -qty
    return 0.0


def _price_and_size(trade: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    try:
        price = float(trade.get("price"))
    except Exception:
        price = None
    try:
        size = float(trade.get("size") or 0)
    except Exception:
        size = 0.0
    if price is None or size <= 0:
        return None
    return price, size


def _coerce_float(val: Any) -> Optional[float]:
    try:
        return float(val)
    except Exception:
        return None


def _compute_entry_exit_prices(trades: List[Dict[str, Any]], meta: Dict[str, Any]):
    if not trades:
        return None, None, None, None

    entry_trades: List[Tuple[float, float]] = []
    exit_trades: List[Tuple[float, float]] = []

    first_with_qty = next((t for t in trades if _signed_qty(t) != 0), None)
    if not first_with_qty:
        return None, None, None, None

    initial_sign = 1 if _signed_qty(first_with_qty) > 0 else -1
    pos = 0.0
    phase = "entry"

    for trade in trades:
        delta = _signed_qty(trade)
        if delta == 0:
            continue
        delta_sign = 1 if delta > 0 else -1
        ps = _price_and_size(trade)

        if phase == "entry":
            if (pos == 0 and delta_sign == initial_sign) or (
                pos != 0 and delta_sign == initial_sign and (pos + delta) * initial_sign > 0
            ):
                if ps:
                    entry_trades.append((ps[0], ps[1]))
                pos += delta
                continue
            phase = "exit"

        if ps:
            exit_trades.append((ps[0], ps[1]))
        pos += delta
        if phase == "exit" and abs(pos) < 1e-9:
            break

    def _vwap(pairs: List[Tuple[float, float]]) -> Optional[float]:
        if not pairs:
            return None
        total = sum(p * s for p, s in pairs)
        vol = sum(s for _, s in pairs)
        if vol <= 0:
            return None
        return total / vol

    entry_vwap = _vwap(entry_trades)
    exit_vwap = _vwap(exit_trades)

    entry_source = "fills_vwap" if entry_vwap is not None else None
    exit_source = "fills_vwap" if exit_vwap is not None else None

    if entry_vwap is None:
        meta_entry = _coerce_float(meta.get("entry_price"))
        if meta_entry is not None:
            entry_vwap = meta_entry
            entry_source = "meta_entry_price"
        else:
            ps_first = _price_and_size(trades[0])
            if ps_first:
                entry_vwap = ps_first[0]
                entry_source = "first_trade_price"

    if exit_vwap is None:
        ps_last = None
        for trade in reversed(trades):
            ps_last = _price_and_size(trade)
            if ps_last:
                break
        if ps_last:
            exit_vwap = ps_last[0]
            exit_source = "last_trade_price"

    return entry_vwap, exit_vwap, entry_source, exit_source


def backfill(limit: int = 500):
    sb = get_supabase_client()
    resp = sb.table("trade_results").select("id,raw_trades,entry_price,exit_price,comment").limit(limit).execute()
    updated = 0
    for row in resp.data or []:
        if row.get("entry_price") is not None and row.get("exit_price") is not None:
            continue
        raw_trades = row.get("raw_trades")
        trades: List[Dict[str, Any]] = []
        if isinstance(raw_trades, str):
            try:
                trades = json.loads(raw_trades)
            except Exception:
                logger.warning("Row %s has invalid raw_trades string", row.get("id"))
        elif isinstance(raw_trades, list):
            trades = raw_trades

        entry_price, exit_price, entry_source, exit_source = _compute_entry_exit_prices(trades, row)
        if entry_price is None and exit_price is None:
            continue

        update_payload = {}
        if entry_price is not None:
            update_payload["entry_price"] = entry_price
            update_payload["entry_price_source"] = entry_source
        if exit_price is not None:
            update_payload["exit_price"] = exit_price
            update_payload["exit_price_source"] = exit_source

        if not update_payload:
            continue

        sb.table("trade_results").update(update_payload).eq("id", row["id"]).execute()
        updated += 1
        logger.info("Updated row %s with entry_price=%s exit_price=%s", row.get("id"), entry_price, exit_price)

    logger.info("Backfill complete. Updated %s rows", updated)


if __name__ == "__main__":
    backfill()
