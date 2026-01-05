"""One-off helper to backfill entry/exit prices on historical trade_results rows.

This is intentionally *not* invoked automatically. Run manually when needed:
    python scripts/backfill_trade_prices.py
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Tuple

from config import load_config
from supabase import create_client

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

config = load_config()
SUPABASE_URL = config["SUPABASE_URL"]
SUPABASE_KEY = config["SUPABASE_KEY"]


def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase credentials missing; cannot backfill")
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def _to_float(val) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _signed_qty(trade: Dict) -> float:
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


def _vwap(trades: List[Dict]) -> Optional[float]:
    numer = 0.0
    denom = 0.0
    for t in trades:
        price = _to_float(t.get("price"))
        size = _to_float(t.get("size"))
        if price is None or size is None or size <= 0:
            continue
        numer += price * size
        denom += size
    return numer / denom if denom > 0 else None


def derive_prices(trades: List[Dict], meta: Optional[Dict] = None) -> Tuple[Optional[float], Optional[float], Optional[str], Optional[str]]:
    meta = meta or {}
    entry_trades: List[Dict] = []
    exit_trades: List[Dict] = []
    initial_sign: Optional[int] = None
    pos = 0.0

    for t in trades:
        delta = _signed_qty(t)
        if delta == 0:
            continue

        sign = 1 if delta > 0 else -1
        if initial_sign is None:
            initial_sign = sign

        if abs(pos) < 1e-9:
            bucket = "entry" if sign == initial_sign else "exit"
        else:
            prospective = pos + delta
            if sign == initial_sign and abs(prospective) > abs(pos):
                bucket = "entry"
            else:
                bucket = "exit"

        if bucket == "entry":
            entry_trades.append(t)
        else:
            exit_trades.append(t)

        pos += delta

    entry_vwap = _vwap(entry_trades)
    exit_vwap = _vwap(exit_trades)

    entry_price_source = None
    exit_price_source = None

    entry_price = entry_vwap
    if entry_price is not None:
        entry_price_source = "fills_entry_vwap"
    else:
        meta_entry = _to_float(meta.get("entry_price")) if meta else None
        if meta_entry is not None:
            entry_price = meta_entry
            entry_price_source = "meta_entry_price"
        else:
            first_price = _to_float(trades[0].get("price")) if trades else None
            if first_price is not None:
                entry_price = first_price
                entry_price_source = "first_trade_price"

    exit_price = exit_vwap
    if exit_price is not None:
        exit_price_source = "fills_exit_vwap"
    else:
        last_price = _to_float(trades[-1].get("price")) if trades else None
        if last_price is not None:
            exit_price = last_price
            exit_price_source = "last_trade_price"

    return entry_price, exit_price, entry_price_source, exit_price_source


def _parse_trades(raw_trades) -> List[Dict]:
    if isinstance(raw_trades, list):
        return [t for t in raw_trades if isinstance(t, dict)]
    if isinstance(raw_trades, str):
        try:
            parsed = json.loads(raw_trades)
            if isinstance(parsed, list):
                return [t for t in parsed if isinstance(t, dict)]
        except Exception:
            return []
    return []


def main():
    sb = _get_supabase()
    resp = sb.table("trade_results").select("id,raw_trades,entry_price,exit_price,comment").execute()
    rows = resp.data or []
    updates = 0

    for row in rows:
        if row.get("entry_price") is not None and row.get("exit_price") is not None:
            continue

        trades = _parse_trades(row.get("raw_trades"))
        if not trades:
            continue

        entry_price, exit_price, entry_src, exit_src = derive_prices(trades)
        if entry_price is None and exit_price is None:
            continue

        payload = {}
        if entry_price is not None:
            payload["entry_price"] = entry_price
            payload["entry_price_source"] = entry_src
        if exit_price is not None:
            payload["exit_price"] = exit_price
            payload["exit_price_source"] = exit_src

        if not payload:
            continue

        sb.table("trade_results").update(payload).eq("id", row.get("id")).execute()
        updates += 1
        logging.info("Updated trade_result id=%s entry_price=%s exit_price=%s", row.get("id"), entry_price, exit_price)

    logging.info("Backfill complete; updated %s rows", updates)


if __name__ == "__main__":
    main()
