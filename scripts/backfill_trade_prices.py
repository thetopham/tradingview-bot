"""One-off helper to backfill entry/exit prices on existing trade_results rows.

Usage:
    python scripts/backfill_trade_prices.py

The script is intentionally conservative: it skips rows without raw_trades
and only updates rows missing entry/exit prices.
"""

import json
import logging
from typing import Any, Dict, List, Tuple

from api import get_supabase_client

logging.basicConfig(level=logging.INFO)


def _safe_float(val: Any):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


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


def _compute_vwap(trades: List[Dict[str, Any]]) -> float | None:
    numer = 0.0
    denom = 0.0
    for t in trades:
        price = _safe_float(t.get("price"))
        size = _safe_float(t.get("size"))
        if price is None or size is None or size <= 0:
            continue
        numer += price * size
        denom += size
    if denom == 0:
        return None
    return numer / denom


def derive_entry_exit(trades: List[Dict[str, Any]], meta: Dict[str, Any] | None = None) -> Tuple[Any, Any, Any, Any]:
    meta = meta or {}
    if not trades:
        return None, None, None, None

    initial_sign = None
    pos = 0.0
    entry_trades: List[Dict[str, Any]] = []
    exit_trades: List[Dict[str, Any]] = []
    exiting = False

    for t in trades:
        delta = _signed_qty(t)
        if delta == 0:
            continue
        sign = 1 if delta > 0 else -1
        if initial_sign is None:
            initial_sign = sign

        if not exiting:
            next_pos = pos + delta
            if sign == initial_sign and (pos == 0 or abs(next_pos) > abs(pos)):
                entry_trades.append(t)
                pos = next_pos
                continue
            exiting = True

        pos += delta
        exit_trades.append(t)

    entry_vwap = _compute_vwap(entry_trades)
    exit_vwap = _compute_vwap(exit_trades)

    entry_source = None
    exit_source = None

    if entry_vwap is None:
        meta_entry = _safe_float(meta.get("entry_price"))
        if meta_entry is not None:
            entry_vwap = meta_entry
            entry_source = "meta_entry_price"
        else:
            first_trade = trades[0] if trades else None
            first_price = _safe_float(first_trade.get("price")) if isinstance(first_trade, dict) else None
            if first_price is not None:
                entry_vwap = first_price
                entry_source = "first_trade_price"
    else:
        entry_source = "trade_fills_vwap"

    if exit_vwap is None:
        last_trade = trades[-1] if trades else None
        last_price = _safe_float(last_trade.get("price")) if isinstance(last_trade, dict) else None
        if last_price is not None:
            exit_vwap = last_price
            exit_source = "last_trade_price"
    else:
        exit_source = "trade_fills_vwap"

    return entry_vwap, exit_vwap, entry_source, exit_source


def main():
    sb = get_supabase_client()
    logging.info("Fetching trade_results rows for backfill...")
    resp = sb.table("trade_results").select("id,raw_trades,entry_price,exit_price,entry_price_source,exit_price_source").limit(2000).execute()
    rows = resp.data or []

    updated = 0
    for row in rows:
        if row.get("entry_price") is not None and row.get("exit_price") is not None:
            continue

        raw_trades = row.get("raw_trades")
        if not raw_trades:
            continue

        if isinstance(raw_trades, str):
            try:
                raw_trades = json.loads(raw_trades)
            except Exception:
                logging.warning("Row %s has unparsable raw_trades", row.get("id"))
                continue

        if not isinstance(raw_trades, list):
            continue

        entry_price, exit_price, entry_source, exit_source = derive_entry_exit(raw_trades)
        updates = {}
        if entry_price is not None and row.get("entry_price") is None:
            updates["entry_price"] = entry_price
            updates["entry_price_source"] = entry_source
        if exit_price is not None and row.get("exit_price") is None:
            updates["exit_price"] = exit_price
            updates["exit_price_source"] = exit_source

        if updates:
            sb.table("trade_results").update(updates).eq("id", row.get("id")).execute()
            updated += 1
            logging.info("Updated row %s with entry_price=%s exit_price=%s", row.get("id"), updates.get("entry_price"), updates.get("exit_price"))

    logging.info("Backfill complete: %s rows updated", updated)


if __name__ == "__main__":
    main()
