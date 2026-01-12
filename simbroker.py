"""Simulation broker with persistent JSON state.

Implements a minimal ProjectX-compatible surface for simulated accounts,
orders, positions, trades, and bracket links. This module is intentionally
self-contained and not wired into api.py yet.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from supabase import create_client

SIM_STATE_PATH = "sim_state.json"

_STATE_LOCK = threading.RLock()


def _now_iso_utc() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _round_to_tick(price: float, tick_size: float) -> float:
    """Round a price to the nearest tick size."""
    if tick_size == 0:
        return price
    return round(price / tick_size) * tick_size


def _load_state(state_path: str) -> dict[str, Any]:
    """Load the simulation state from disk, returning defaults if missing."""
    if not os.path.exists(state_path):
        return {
            "nextOrderId": 1,
            "nextPositionId": 1,
            "nextTradeId": 1,
            "accounts": [],
            "orders": [],
            "positions": [],
            "trades": [],
            "last_processed_bar_ts": {},
            "bracket_links": {},
        }

    with open(state_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _save_state_atomic(state_path: str, state: dict[str, Any]) -> None:
    """Persist state to disk using atomic replace."""
    tmp_path = f"{state_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(tmp_path, state_path)


def _normalize_ts_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        if isinstance(value, (int, float)):
            ts_value = float(value)
            if ts_value > 1e12:
                ts_value /= 1000.0
            return datetime.fromtimestamp(ts_value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None
    return None


def symbol_from_contract(contractId: str) -> str:
    if not contractId:
        return ""
    upper = contractId.upper()
    if ".MES." in upper:
        return "MES"
    tokens = [token.strip() for token in contractId.split(".") if token.strip()]
    ignored = {"CON", "F", "US"}
    symbol_candidates = [token for token in tokens if token.isalpha() and token.upper() not in ignored]
    if symbol_candidates:
        return symbol_candidates[-1].upper()
    for token in tokens:
        if any(char.isalpha() for char in token):
            return "".join(char for char in token if char.isalpha()).upper()
    return upper


class PriceFeed:
    def get_latest_close(
        self,
        symbol: str,
        timeframe_preference: Sequence[str] | None = None,
    ) -> tuple[float | None, str | None]:
        raise NotImplementedError

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start_ts_iso: str,
        end_ts_iso: str,
        limit: int = 20000,
    ) -> list[dict]:
        raise NotImplementedError


class SupabasePriceFeed(PriceFeed):
    def __init__(self, config: dict[str, Any] | None = None, table: str = "tv_datafeed") -> None:
        self.table = table
        self.supabase_url = (config or {}).get("SUPABASE_URL") or os.getenv("SUPABASE_URL")
        self.supabase_key = (config or {}).get("SUPABASE_KEY") or os.getenv("SUPABASE_KEY")
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self.supabase_url or not self.supabase_key:
            return None
        try:
            self._client = create_client(self.supabase_url, self.supabase_key)
        except Exception as exc:
            logging.warning("Supabase client init failed: %s", exc)
            return None
        return self._client

    def get_latest_close(
        self,
        symbol: str,
        timeframe_preference: Sequence[str] | None = None,
    ) -> tuple[float | None, str | None]:
        client = self._get_client()
        if not client or not symbol:
            return None, None
        preferences = list(timeframe_preference or ["1m", "5m"])
        for timeframe in preferences:
            try:
                result = (
                    client.table(self.table)
                    .select("ts,c")
                    .eq("symbol", symbol)
                    .eq("timeframe", timeframe)
                    .order("ts", desc=True)
                    .limit(1)
                    .execute()
                )
                rows = result.data or []
                if not rows:
                    continue
                row = rows[0]
                close = row.get("c")
                ts_iso = _normalize_ts_iso(row.get("ts"))
                if close is None:
                    continue
                return float(close), ts_iso
            except Exception as exc:
                logging.debug("Supabase latest close fetch failed (%s %s): %s", symbol, timeframe, exc)
                continue
        return None, None

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start_ts_iso: str,
        end_ts_iso: str,
        limit: int = 20000,
    ) -> list[dict]:
        client = self._get_client()
        if not client or not symbol or not timeframe:
            return []
        try:
            result = (
                client.table(self.table)
                .select("ts,o,h,l,c,v")
                .eq("symbol", symbol)
                .eq("timeframe", timeframe)
                .gte("ts", start_ts_iso)
                .lte("ts", end_ts_iso)
                .order("ts", desc=False)
                .limit(limit)
                .execute()
            )
            rows = result.data or []
        except Exception as exc:
            logging.debug("Supabase bar fetch failed (%s %s): %s", symbol, timeframe, exc)
            return []
        bars = []
        for row in rows:
            ts_iso = _normalize_ts_iso(row.get("ts"))
            bars.append(
                {
                    "t": ts_iso,
                    "o": row.get("o"),
                    "h": row.get("h"),
                    "l": row.get("l"),
                    "c": row.get("c"),
                    "v": row.get("v"),
                }
            )
        return bars


class InMemoryPriceFeed(PriceFeed):
    def __init__(self, bars_by_symbol_timeframe: dict) -> None:
        self.bars_by_symbol_timeframe = bars_by_symbol_timeframe or {}

    def _get_bars_for(self, symbol: str, timeframe: str) -> list[dict]:
        if not symbol or not timeframe:
            return []
        if isinstance(self.bars_by_symbol_timeframe, dict):
            by_symbol = self.bars_by_symbol_timeframe.get(symbol) or self.bars_by_symbol_timeframe.get(symbol.upper())
            if isinstance(by_symbol, dict):
                return list(by_symbol.get(timeframe, []))
            key = (symbol, timeframe)
            if key in self.bars_by_symbol_timeframe:
                return list(self.bars_by_symbol_timeframe.get(key, []))
        return []

    def get_latest_close(
        self,
        symbol: str,
        timeframe_preference: Sequence[str] | None = None,
    ) -> tuple[float | None, str | None]:
        preferences = list(timeframe_preference or ["1m", "5m"])
        for timeframe in preferences:
            bars = self._get_bars_for(symbol, timeframe)
            if not bars:
                continue
            latest = bars[-1]
            close = latest.get("c")
            ts_iso = latest.get("t") or latest.get("ts")
            if close is None:
                continue
            return float(close), _normalize_ts_iso(ts_iso)
        return None, None

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start_ts_iso: str,
        end_ts_iso: str,
        limit: int = 20000,
    ) -> list[dict]:
        bars = self._get_bars_for(symbol, timeframe)
        if not bars:
            return []
        filtered = []
        for bar in bars:
            ts = bar.get("t") or bar.get("ts")
            ts_iso = _normalize_ts_iso(ts)
            if ts_iso is None:
                continue
            if start_ts_iso <= ts_iso <= end_ts_iso:
                filtered.append(
                    {
                        "t": ts_iso,
                        "o": bar.get("o"),
                        "h": bar.get("h"),
                        "l": bar.get("l"),
                        "c": bar.get("c"),
                        "v": bar.get("v"),
                    }
                )
            if len(filtered) >= limit:
                break
        return filtered


class SimBroker:
    """Persistent simulation broker for ProjectX-style API requests."""

    def __init__(self, state_path: str, config: dict[str, Any], price_feed: Any | None = None) -> None:
        self.state_path = state_path
        self.config = config
        self.price_feed = price_feed

    def _load_state_locked(self) -> dict[str, Any]:
        with _STATE_LOCK:
            return _load_state(self.state_path)

    def _save_state_locked(self, state: dict[str, Any]) -> None:
        with _STATE_LOCK:
            _save_state_atomic(self.state_path, state)

    def handle_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Route /api/... POST endpoints for the simulation broker."""
        if path == "/api/sim/update":
            account_id = payload.get("accountId")
            contract_id = payload.get("contractId")
            now_ts_iso = payload.get("now_ts_iso")
            if account_id is None or contract_id is None:
                return {
                    "success": False,
                    "errorCode": "INVALID_ARGUMENT",
                    "errorMessage": "accountId and contractId are required",
                }
            return self.sim_update(account_id, contract_id, now_ts_iso)

        return {
            "success": False,
            "errorCode": "NOT_IMPLEMENTED",
            "errorMessage": f"No sim broker handler for path: {path}",
        }

    def sim_update(self, accountId: int, contractId: str, now_ts_iso: str | None = None) -> dict[str, Any]:
        """Update simulated state for an account/contract tuple."""
        now_ts = now_ts_iso or _now_iso_utc()

        with _STATE_LOCK:
            state = _load_state(self.state_path)
            state.setdefault("last_processed_bar_ts", {})
            state.setdefault("bracket_links", {})
            state.setdefault("orders", [])
            state.setdefault("positions", [])
            state.setdefault("trades", [])
            state.setdefault("accounts", [])
            state.setdefault("nextOrderId", 1)
            state.setdefault("nextPositionId", 1)
            state.setdefault("nextTradeId", 1)

            key = f"{accountId}:{contractId}"
            state["last_processed_bar_ts"][key] = now_ts

            _save_state_atomic(self.state_path, state)

        return {
            "success": True,
            "errorCode": None,
            "errorMessage": None,
            "accountId": accountId,
            "contractId": contractId,
            "lastProcessedBarTs": now_ts,
        }

    def get_state_snapshot(self) -> dict[str, Any]:
        """Return a copy of the current state (debug helper)."""
        with _STATE_LOCK:
            state = _load_state(self.state_path)
        return copy.deepcopy(state)
