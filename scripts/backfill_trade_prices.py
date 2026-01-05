"""Backfill entry/exit prices on trade_results using stored raw_trades.

Usage:
    export SUPABASE_URL=...
    export SUPABASE_KEY=...
    python scripts/backfill_trade_prices.py
"""

import json
from typing import Dict, List, Tuple

from supabase import Client, create_client


def _signed_qty(trade: dict) -> float:
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


def _compute_vwap(trades: List[dict]) -> float | None:
    num = 0.0
    den = 0.0
    for t in trades:
        try:
            price = float(t.get("price"))
            size = abs(float(t.get("size") or 0))
        except Exception:
            continue
        if price is None or size <= 0:
            continue
        num += price * size
        den += size
    return num / den if den else None


def _derive_entry_exit_prices(trades: List[dict], meta: Dict) -> Tuple[Tuple[float | None, str | None], Tuple[float | None, str | None]]:
    entry_trades: List[dict] = []
    exit_trades: List[dict] = []
    pos = 0.0
    initial_sign: int | None = None

    for t in trades:
        delta = _signed_qty(t)
        if delta == 0:
            continue
        sign = 1 if delta > 0 else -1
        if initial_sign is None:
            initial_sign = sign

        if pos == 0:
            (entry_trades if sign == initial_sign else exit_trades).append(t)
            pos += delta
            continue

        pos_after = pos + delta
        if sign == initial_sign and abs(pos_after) >= abs(pos):
            entry_trades.append(t)
        else:
            exit_trades.append(t)
        pos = pos_after

    entry_price = _compute_vwap(entry_trades)
    exit_price = _compute_vwap(exit_trades)
    entry_source = "fills_vwap" if entry_price is not None else None
    exit_source = "fills_vwap" if exit_price is not None else None

    if entry_price is None:
        meta_entry = meta.get("entry_price")
        try:
            if meta_entry is not None:
                entry_price = float(meta_entry)
                entry_source = "meta_entry_price"
        except Exception:
            pass
    if entry_price is None and trades:
        first_price = trades[0].get("price")
        try:
            if first_price is not None:
                entry_price = float(first_price)
                entry_source = "first_trade_price"
        except Exception:
            pass

    if exit_price is None and trades:
        last_price = trades[-1].get("price")
        try:
            if last_price is not None:
                exit_price = float(last_price)
                exit_source = "last_trade_price"
        except Exception:
            pass

    return (entry_price, entry_source), (exit_price, exit_source)


def _safe_load_raw_trades(raw) -> List[dict]:
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, dict)]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [t for t in data if isinstance(t, dict)]
        except Exception:
            return []
    return []


def main():
    import os

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    supabase: Client = create_client(url, key)

    page = 0
    page_size = 200
    updated = 0

    while True:
        start = page * page_size
        end = start + page_size - 1
        resp = (
            supabase.table("trade_results")
            .select("id,raw_trades,entry_price,exit_price,comment")
            .is_("entry_price", None)
            .range(start, end)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            break

        for row in rows:
            trades = _safe_load_raw_trades(row.get("raw_trades") or [])
            (entry_price, entry_source), (exit_price, exit_source) = _derive_entry_exit_prices(trades, {})
            updates = {}
            if entry_price is not None:
                updates["entry_price"] = entry_price
                updates["entry_price_source"] = entry_source
            if exit_price is not None:
                updates["exit_price"] = exit_price
                updates["exit_price_source"] = exit_source
            if updates:
                supabase.table("trade_results").update(updates).eq("id", row["id"]).execute()
                updated += 1

        page += 1

    print(f"Updated {updated} rows with entry/exit prices")


if __name__ == "__main__":
    main()
