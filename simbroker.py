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
        self._ensure_accounts()

    def _load_state_locked(self) -> dict[str, Any]:
        with _STATE_LOCK:
            return _load_state(self.state_path)

    def _save_state_locked(self, state: dict[str, Any]) -> None:
        with _STATE_LOCK:
            _save_state_atomic(self.state_path, state)

    def handle_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Route /api/... POST endpoints for the simulation broker."""
        if path == "/api/Account/search":
            only_active = payload.get("onlyActiveAccounts")
            return self.search_accounts(only_active)
        if path == "/api/Auth/loginKey":
            return self.auth_login_key(payload)
        if path == "/api/Auth/validate":
            return self.auth_validate(payload)
        if path == "/api/Contract/available":
            return self.contract_available(payload)
        if path == "/api/Contract/search":
            return self.contract_search(payload)
        if path == "/api/Contract/searchById":
            return self.contract_search_by_id(payload)
        if path == "/api/History/retrieveBars":
            return self.history_retrieve_bars(payload)
        if path == "/api/Order/place":
            return self.place_order(payload)
        if path == "/api/Order/search":
            return self.search_orders(payload)
        if path == "/api/Order/searchOpen":
            return self.search_open_orders(payload)
        if path == "/api/Order/cancel":
            return self.cancel_order(payload)
        if path == "/api/Order/modify":
            return self.modify_order(payload)
        if path == "/api/Position/closeContract":
            return self.close_contract(payload)
        if path == "/api/Position/partialCloseContract":
            return self.partial_close_contract(payload)
        if path == "/api/Position/searchOpen":
            return self.search_open_positions(payload)
        if path == "/api/Trade/search":
            return self.search_trades(payload)
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

    def _response_success(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = {"success": True, "errorCode": 0, "errorMessage": None}
        response.update(payload)
        return response

    def _response_error(self, code: str, message: str) -> dict[str, Any]:
        return {"success": False, "errorCode": code, "errorMessage": message}

    def _normalize_ts(self, value: Any) -> str | None:
        return _normalize_ts_iso(value)

    def _filter_by_time_range(
        self,
        records: Iterable[dict[str, Any]],
        start_ts: Any,
        end_ts: Any,
        timestamp_key: str,
    ) -> list[dict[str, Any]]:
        normalized_start = self._normalize_ts(start_ts) if start_ts is not None else None
        normalized_end = self._normalize_ts(end_ts) if end_ts is not None else None
        if normalized_start is None and normalized_end is None:
            return list(records)
        filtered = []
        for record in records:
            record_ts = record.get(timestamp_key) or record.get("timestamp")
            normalized_record_ts = self._normalize_ts(record_ts) or record_ts
            if normalized_record_ts is None:
                continue
            if normalized_start is not None and normalized_record_ts < normalized_start:
                continue
            if normalized_end is not None and normalized_record_ts > normalized_end:
                continue
            filtered.append(record)
        return filtered

    def _build_contract(self, contract_id: str) -> dict[str, Any]:
        symbol = symbol_from_contract(contract_id)
        default_tick_size = float(self.config.get("SIM_DEFAULT_TICK_SIZE", 0.25))
        default_tick_value = float(self.config.get("SIM_DEFAULT_TICK_VALUE", 1.25))
        if symbol.upper() == "MES":
            tick_size = 0.25
            tick_value = 1.25
        else:
            tick_size = default_tick_size
            tick_value = default_tick_value
        return {
            "id": contract_id,
            "name": symbol or contract_id,
            "description": f"{symbol} Futures" if symbol else contract_id,
            "tickSize": tick_size,
            "tickValue": tick_value,
            "activeContract": True,
            "symbolId": symbol or contract_id,
        }

    def _gather_contract_ids(self, payload: dict[str, Any]) -> list[str]:
        contract_ids: list[str] = []
        config_contract_id = self.config.get("OVERRIDE_CONTRACT_ID") or "CON.F.US.MES.H26"
        if config_contract_id:
            contract_ids.append(config_contract_id)
        payload_contract_id = payload.get("contractId") or payload.get("id")
        if payload_contract_id:
            contract_ids.append(payload_contract_id)
        payload_ids = payload.get("contractIds")
        if isinstance(payload_ids, list):
            contract_ids.extend([cid for cid in payload_ids if cid])
        unique = []
        for cid in contract_ids:
            if cid not in unique:
                unique.append(cid)
        return unique

    def auth_login_key(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = payload.get("token") or "sim-token"
        return self._response_success({"token": token, "newToken": token})

    def auth_validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = payload.get("token") or payload.get("newToken") or "sim-token"
        return self._response_success({"newToken": token})

    def contract_available(self, payload: dict[str, Any]) -> dict[str, Any]:
        contracts = [self._build_contract(cid) for cid in self._gather_contract_ids(payload)]
        return self._response_success({"contracts": contracts})

    def contract_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        search_text = (payload.get("searchText") or payload.get("searchTerm") or "").strip().lower()
        contracts = [self._build_contract(cid) for cid in self._gather_contract_ids(payload)]
        if search_text:
            filtered = []
            for contract in contracts:
                haystack = " ".join(
                    str(contract.get(key) or "").lower()
                    for key in ("id", "name", "description", "symbolId")
                )
                if search_text in haystack:
                    filtered.append(contract)
            contracts = filtered
        return self._response_success({"contracts": contracts})

    def contract_search_by_id(self, payload: dict[str, Any]) -> dict[str, Any]:
        contract_ids = self._gather_contract_ids(payload)
        contracts = [self._build_contract(cid) for cid in contract_ids]
        contract = contracts[0] if contracts else None
        return self._response_success({"contract": contract})

    def history_retrieve_bars(self, payload: dict[str, Any]) -> dict[str, Any]:
        contract_id = payload.get("contractId")
        unit = payload.get("unit")
        unit_number = payload.get("unitNumber")
        start_ts = payload.get("startTime") or payload.get("startTimestamp")
        end_ts = payload.get("endTime") or payload.get("endTimestamp")
        if not contract_id:
            return self._response_error("INVALID_ARGUMENT", "contractId is required")
        if unit is None:
            return self._response_error("INVALID_ARGUMENT", "unit is required")
        try:
            unit_value = int(unit)
        except (TypeError, ValueError):
            unit_value = None
        if unit_value not in (2,):
            return self._response_error("INVALID_ARGUMENT", "Only Minute unit (2) is supported")
        if unit_number not in (1, 5, "1", "5"):
            return self._response_error("INVALID_ARGUMENT", "Only 1 or 5 minute bars are supported")
        unit_number = int(unit_number)
        start_ts_iso = self._normalize_ts(start_ts)
        end_ts_iso = self._normalize_ts(end_ts)
        if not start_ts_iso or not end_ts_iso:
            return self._response_error("INVALID_ARGUMENT", "startTimestamp and endTimestamp are required")

        symbol = symbol_from_contract(contract_id)
        timeframe = f"{unit_number}m"
        bars = self._get_price_feed().get_bars(symbol, timeframe, start_ts_iso, end_ts_iso)
        return self._response_success({"bars": bars})

    def _ensure_accounts(self) -> None:
        accounts_config = self.config.get("ACCOUNTS") or {}
        bracket_overrides = self.config.get("SIM_ACCOUNT_BRACKETS") or {}
        if not isinstance(accounts_config, dict) or not accounts_config:
            return
        starting_balance = float(self.config.get("SIM_STARTING_BALANCE", 0.0))

        with _STATE_LOCK:
            state = _load_state(self.state_path)
            accounts = list(state.get("accounts") or [])
            accounts_by_id = {
                account.get("id"): account
                for account in accounts
                if account.get("id") is not None
            }
            updated = False
            for account_name, account_id in accounts_config.items():
                if account_id is None:
                    continue
                bracket_override = bracket_overrides.get(str(account_name).lower(), {})
                bracket_sl_usd = float(bracket_override.get("sl_usd", self.config.get("SIM_BRACKET_SL_USD", 0.0)))
                bracket_tp_usd = float(bracket_override.get("tp_usd", self.config.get("SIM_BRACKET_TP_USD", 0.0)))
                existing = accounts_by_id.get(account_id)
                if existing is None:
                    accounts.append(
                        {
                            "id": account_id,
                            "name": account_name,
                            "balance": starting_balance,
                            "canTrade": True,
                            "isVisible": True,
                            "simulated": True,
                            "bracketSlUsd": bracket_sl_usd,
                            "bracketTpUsd": bracket_tp_usd,
                        }
                    )
                    updated = True
                    continue
                if existing.get("name") != account_name:
                    existing["name"] = account_name
                    updated = True
                if existing.get("bracketSlUsd") != bracket_sl_usd:
                    existing["bracketSlUsd"] = bracket_sl_usd
                    updated = True
                if existing.get("bracketTpUsd") != bracket_tp_usd:
                    existing["bracketTpUsd"] = bracket_tp_usd
                    updated = True
                if "balance" not in existing:
                    existing["balance"] = starting_balance
                    updated = True
                if "canTrade" not in existing:
                    existing["canTrade"] = True
                    updated = True
                if "isVisible" not in existing:
                    existing["isVisible"] = True
                    updated = True
                if "simulated" not in existing:
                    existing["simulated"] = True
                    updated = True

            if updated:
                state["accounts"] = accounts
                _save_state_atomic(self.state_path, state)

    def search_accounts(self, only_active_accounts: bool | None = None) -> dict[str, Any]:
        with _STATE_LOCK:
            state = _load_state(self.state_path)
        accounts = []
        for account in state.get("accounts", []):
            can_trade = bool(account.get("canTrade", True))
            is_visible = bool(account.get("isVisible", True))
            if only_active_accounts and not (can_trade and is_visible):
                continue
            accounts.append(
                {
                    "id": account.get("id"),
                    "name": account.get("name"),
                    "balance": account.get("balance", 0.0),
                    "canTrade": can_trade,
                    "isVisible": is_visible,
                }
            )
        return self._response_success({"accounts": accounts})

    def sim_update(self, accountId: int, contractId: str, now_ts_iso: str | None = None) -> dict[str, Any]:
        """Update simulated state for an account/contract tuple."""
        now_ts = _normalize_ts_iso(now_ts_iso) or _now_iso_utc()
        old_ts = "1970-01-01T00:00:00Z"
        closed: list[dict[str, Any]] = []

        symbol = symbol_from_contract(contractId)
        price_feed = self._get_price_feed()
        preferred_timeframe = (self.config.get("SIM_PRICE_TIMEFRAME") or "1m").strip() or "1m"
        fallback_timeframe = "5m"

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
            last_ts = state["last_processed_bar_ts"].get(key) or old_ts

            bars = price_feed.get_bars(symbol, preferred_timeframe, last_ts, now_ts)
            if not bars and preferred_timeframe != fallback_timeframe:
                bars = price_feed.get_bars(symbol, fallback_timeframe, last_ts, now_ts)

            latest_processed_ts = last_ts
            if bars:
                orders = state.get("orders", [])
                orders_by_id = {order.get("id"): order for order in orders if order.get("id") is not None}
                positions = state.get("positions", [])

                for bar in bars:
                    bar_ts = _normalize_ts_iso(bar.get("t") or bar.get("ts"))
                    if bar_ts is None or bar_ts <= last_ts:
                        continue

                    latest_processed_ts = bar_ts
                    bar_high = bar.get("h")
                    bar_low = bar.get("l")
                    if bar_high is None or bar_low is None:
                        last_ts = bar_ts
                        continue

                    open_orders = [
                        order
                        for order in orders
                        if (
                            order.get("accountId") == accountId
                            and order.get("contractId") == contractId
                            and int(order.get("status", 0)) == 1
                            and order.get("parentOrderId") is None
                            and int(order.get("type", 0)) in (1, 4)
                        )
                    ]
                    if open_orders:
                        position = next(
                            (
                                existing
                                for existing in positions
                                if (
                                    existing.get("accountId") == accountId
                                    and existing.get("contractId") == contractId
                                    and float(existing.get("size", 0) or 0) != 0
                                )
                            ),
                            None,
                        )
                        for order in open_orders:
                            order_type = int(order.get("type", 0))
                            side = int(order.get("side", 0))
                            limit_px = order.get("limitPrice")
                            stop_px = order.get("stopPrice")
                            if order_type == 1:
                                trigger_price = limit_px
                                if trigger_price is None:
                                    continue
                                if side == 0 and float(bar_low) > float(trigger_price):
                                    continue
                                if side == 1 and float(bar_high) < float(trigger_price):
                                    continue
                            else:
                                trigger_price = stop_px
                                if trigger_price is None:
                                    continue
                                if side == 0 and float(bar_high) < float(trigger_price):
                                    continue
                                if side == 1 and float(bar_low) > float(trigger_price):
                                    continue

                            fill_price = float(trigger_price)
                            order["status"] = 2
                            order["fillVolume"] = float(order.get("size", 0) or 0)
                            order["filledPrice"] = fill_price
                            order["updateTimestamp"] = bar_ts
                            orders_by_id[order.get("id")] = order

                            fill_size = float(order.get("size", 0) or 0)
                            if fill_size <= 0:
                                continue

                            is_buy = side == 0
                            order_position_type = 1 if is_buy else 2
                            position_size = float(position.get("size", 0) or 0) if position else 0.0
                            position_type = int(position.get("type", 0)) if position else 0

                            closing_size = 0.0
                            pnl = None
                            tick_size = float(self.config.get("SIM_DEFAULT_TICK_SIZE", 0.25))
                            tick_value = float(self.config.get("SIM_DEFAULT_TICK_VALUE", 1.25))
                            if position and position_size and position_type != order_position_type:
                                closing_size = min(position_size, fill_size)
                                entry_price = float(position.get("averagePrice") or 0.0)
                                sign = 1 if position_type == 1 else -1
                                if tick_size:
                                    pnl = ((fill_price - entry_price) / tick_size) * tick_value * closing_size * sign
                                else:
                                    pnl = 0.0
                                remaining_position = position_size - closing_size
                                if remaining_position > 0:
                                    position["size"] = remaining_position
                                else:
                                    positions.remove(position)
                                    position = None

                            residual_size = fill_size - closing_size
                            if residual_size > 0:
                                if position and position_type == order_position_type:
                                    total_size = position_size + residual_size
                                    if total_size:
                                        position["averagePrice"] = (
                                            (position_size * float(position.get("averagePrice") or 0.0))
                                            + (residual_size * fill_price)
                                        ) / total_size
                                    position["size"] = total_size
                                else:
                                    position_id = state["nextPositionId"]
                                    state["nextPositionId"] += 1
                                    position = {
                                        "id": position_id,
                                        "accountId": accountId,
                                        "contractId": contractId,
                                        "contractSymbol": symbol,
                                        "type": order_position_type,
                                        "size": residual_size,
                                        "averagePrice": fill_price,
                                        "creationTimestamp": bar_ts,
                                    }
                                    positions.append(position)

                            trade_id = state["nextTradeId"]
                            state["nextTradeId"] += 1
                            state["trades"].append(
                                {
                                    "id": trade_id,
                                    "accountId": accountId,
                                    "contractId": contractId,
                                    "orderId": order.get("id"),
                                    "price": fill_price,
                                    "side": side,
                                    "size": fill_size,
                                    "profitAndLoss": pnl,
                                    "creationTimestamp": bar_ts,
                                }
                            )

                            if pnl is not None:
                                for account in state.get("accounts", []):
                                    if account.get("id") == accountId:
                                        account["balance"] = float(account.get("balance", 0.0)) + pnl
                                        break

                    position = None
                    for existing in positions:
                        if (
                            existing.get("accountId") == accountId
                            and existing.get("contractId") == contractId
                            and float(existing.get("size", 0) or 0) != 0
                        ):
                            position = existing
                            break
                    if position is None:
                        last_ts = bar_ts
                        continue

                    position_created_ts = _normalize_ts_iso(position.get("creationTimestamp"))
                    if position_created_ts and bar_ts < position_created_ts:
                        last_ts = bar_ts
                        continue

                    parent_id = None
                    sl_order = None
                    tp_order = None
                    for candidate_parent_id, child_ids in (state.get("bracket_links") or {}).items():
                        child_orders = [
                            orders_by_id.get(child_id)
                            for child_id in child_ids
                            if orders_by_id.get(child_id) is not None
                        ]
                        if not child_orders:
                            continue
                        if any(order.get("accountId") != accountId or order.get("contractId") != contractId for order in child_orders):
                            continue
                        if any(int(order.get("status", 0)) != 1 for order in child_orders):
                            continue
                        sl_candidate = next((order for order in child_orders if int(order.get("type", 0)) == 4), None)
                        tp_candidate = next((order for order in child_orders if int(order.get("type", 0)) == 1), None)
                        if sl_candidate and tp_candidate:
                            parent_id = candidate_parent_id
                            sl_order = sl_candidate
                            tp_order = tp_candidate
                            break

                    if sl_order is None or tp_order is None:
                        last_ts = bar_ts
                        continue

                    position_type = int(position.get("type", 0))
                    is_long = position_type == 1
                    tp_price = tp_order.get("limitPrice")
                    sl_price = sl_order.get("stopPrice")
                    if tp_price is None or sl_price is None:
                        last_ts = bar_ts
                        continue

                    if is_long:
                        hit_tp = float(bar_high) >= float(tp_price)
                        hit_sl = float(bar_low) <= float(sl_price)
                    else:
                        hit_tp = float(bar_low) <= float(tp_price)
                        hit_sl = float(bar_high) >= float(sl_price)

                    if not hit_tp and not hit_sl:
                        last_ts = bar_ts
                        continue

                    fill_policy = str(self.config.get("SIM_FILL_POLICY", "worst")).lower()
                    exit_is_tp = hit_tp
                    if hit_tp and hit_sl:
                        exit_is_tp = fill_policy == "best"
                    elif hit_sl:
                        exit_is_tp = False

                    exit_order = tp_order if exit_is_tp else sl_order
                    cancel_order = sl_order if exit_is_tp else tp_order
                    exit_price = float(exit_order.get("limitPrice") if exit_is_tp else exit_order.get("stopPrice") or 0.0)

                    exit_order["status"] = 2
                    exit_order["fillVolume"] = float(position.get("size", 0) or 0)
                    exit_order["filledPrice"] = exit_price
                    exit_order["updateTimestamp"] = bar_ts
                    cancel_order["status"] = 3
                    cancel_order["updateTimestamp"] = bar_ts

                    positions.remove(position)

                    tick_size = float(self.config.get("SIM_DEFAULT_TICK_SIZE", 0.25))
                    tick_value = float(self.config.get("SIM_DEFAULT_TICK_VALUE", 1.25))
                    entry_price = float(position.get("averagePrice") or 0.0)
                    size = float(position.get("size") or 0.0)
                    if tick_size:
                        sign = 1 if is_long else -1
                        pnl = ((exit_price - entry_price) / tick_size) * tick_value * size * sign
                    else:
                        pnl = 0.0

                    exit_order_id = exit_order.get("id")
                    trade_id = state["nextTradeId"]
                    state["nextTradeId"] += 1
                    exit_side = 1 if is_long else 0
                    state["trades"].append(
                        {
                            "id": trade_id,
                            "accountId": accountId,
                            "contractId": contractId,
                            "orderId": exit_order_id,
                            "price": exit_price,
                            "side": exit_side,
                            "size": size,
                            "profitAndLoss": pnl,
                            "creationTimestamp": bar_ts,
                        }
                    )

                    for account in state.get("accounts", []):
                        if account.get("id") == accountId:
                            account["balance"] = float(account.get("balance", 0.0)) + pnl
                            break

                    closed.append(
                        {
                            "accountId": accountId,
                            "contractId": contractId,
                            "exitOrderId": exit_order_id,
                            "exitTradeId": trade_id,
                            "exitPrice": exit_price,
                            "exitTimestamp": bar_ts,
                            "parentOrderId": parent_id,
                        }
                    )
                    last_ts = bar_ts

            if latest_processed_ts != state["last_processed_bar_ts"].get(key):
                state["last_processed_bar_ts"][key] = latest_processed_ts

            _save_state_atomic(self.state_path, state)

        return {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "accountId": accountId,
            "contractId": contractId,
            "lastProcessedBarTs": state["last_processed_bar_ts"].get(key),
            "closed": closed,
        }

    def get_state_snapshot(self) -> dict[str, Any]:
        """Return a copy of the current state (debug helper)."""
        with _STATE_LOCK:
            state = _load_state(self.state_path)
        return copy.deepcopy(state)

    def _get_price_feed(self) -> PriceFeed:
        if self.price_feed is None:
            self.price_feed = SupabasePriceFeed(self.config)
        return self.price_feed

    def _get_bracket_settings(self, state: dict[str, Any], account_id: int) -> tuple[float, float, float, float]:
        sl_usd = float(self.config.get("SIM_BRACKET_SL_USD", 0.0))
        tp_usd = float(self.config.get("SIM_BRACKET_TP_USD", 0.0))
        for account in state.get("accounts", []):
            if account.get("id") == account_id:
                sl_usd = float(account.get("bracketSlUsd", sl_usd))
                tp_usd = float(account.get("bracketTpUsd", tp_usd))
                break
        tick_size = float(self.config.get("SIM_DEFAULT_TICK_SIZE", 0.25))
        tick_value = float(self.config.get("SIM_DEFAULT_TICK_VALUE", 1.25))
        return sl_usd, tp_usd, tick_size, tick_value

    def _resolve_bracket_ticks(
        self,
        payload: dict[str, Any],
        state: dict[str, Any],
        account_id: int,
        tick_value: float,
    ) -> tuple[int, int]:
        sl_ticks = None
        tp_ticks = None
        sl_bracket = payload.get("stopLossBracket") or {}
        tp_bracket = payload.get("takeProfitBracket") or {}
        if isinstance(sl_bracket, dict) and sl_bracket.get("ticks") is not None:
            try:
                sl_ticks = int(sl_bracket.get("ticks"))
            except (TypeError, ValueError):
                sl_ticks = None
        if isinstance(tp_bracket, dict) and tp_bracket.get("ticks") is not None:
            try:
                tp_ticks = int(tp_bracket.get("ticks"))
            except (TypeError, ValueError):
                tp_ticks = None

        if sl_ticks is None or tp_ticks is None:
            sl_usd, tp_usd, _, _ = self._get_bracket_settings(state, account_id)
            if tick_value:
                if sl_ticks is None:
                    sl_ticks = round(sl_usd / tick_value)
                if tp_ticks is None:
                    tp_ticks = round(tp_usd / tick_value)
            else:
                sl_ticks = sl_ticks or 0
                tp_ticks = tp_ticks or 0

        return int(sl_ticks or 0), int(tp_ticks or 0)

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = payload.get("accountId")
        contract_id = payload.get("contractId")
        order_type = payload.get("type")
        side = payload.get("side")
        size = payload.get("size")
        limit_price = payload.get("limitPrice")
        stop_price = payload.get("stopPrice")
        custom_tag = payload.get("customTag")

        if account_id is None or contract_id is None:
            return self._response_error("INVALID_ARGUMENT", "accountId and contractId are required")
        if order_type is None or side is None or size is None:
            return self._response_error("INVALID_ARGUMENT", "type, side, and size are required")
        if int(order_type) not in (1, 2, 4):
            return self._response_error("NOT_IMPLEMENTED", "Only Limit (1), Market (2), or Stop (4) are supported")

        symbol = symbol_from_contract(contract_id)
        price_feed = self._get_price_feed()
        fill_price = None
        if int(order_type) == 2:
            fill_price, _ = price_feed.get_latest_close(symbol, timeframe_preference=["1m", "5m"])
            if fill_price is None:
                return self._response_error("NO_MARKET_DATA", "No market data available for fill")

        now_ts = _now_iso_utc()
        with _STATE_LOCK:
            state = _load_state(self.state_path)
            state.setdefault("orders", [])
            state.setdefault("positions", [])
            state.setdefault("trades", [])
            state.setdefault("bracket_links", {})
            state.setdefault("nextOrderId", 1)
            state.setdefault("nextPositionId", 1)
            state.setdefault("nextTradeId", 1)

            order_id = state["nextOrderId"]
            state["nextOrderId"] += 1
            parent_order = {
                "id": order_id,
                "accountId": account_id,
                "contractId": contract_id,
                "type": int(order_type),
                "side": int(side),
                "size": float(size),
                "limitPrice": limit_price,
                "stopPrice": stop_price,
                "status": 1 if int(order_type) in (1, 4) else 2,
                "fillVolume": float(size) if int(order_type) == 2 else 0,
                "filledPrice": float(fill_price) if int(order_type) == 2 else None,
                "creationTimestamp": now_ts,
                "updateTimestamp": now_ts,
                "customTag": custom_tag,
                "symbolId": symbol,
            }
            state["orders"].append(parent_order)

            if int(order_type) == 2:
                position_type = 1 if int(side) == 0 else 2
                position = None
                for existing in state["positions"]:
                    if existing.get("accountId") == account_id and existing.get("contractId") == contract_id:
                        position = existing
                        break
                if position is None:
                    position_id = state["nextPositionId"]
                    state["nextPositionId"] += 1
                    position = {
                        "id": position_id,
                        "accountId": account_id,
                        "contractId": contract_id,
                        "contractSymbol": symbol,
                    }
                    state["positions"].append(position)
                position.update(
                    {
                        "type": position_type,
                        "size": float(size),
                        "averagePrice": float(fill_price),
                        "creationTimestamp": now_ts,
                    }
                )

                trade_id = state["nextTradeId"]
                state["nextTradeId"] += 1
                state["trades"].append(
                    {
                        "id": trade_id,
                        "accountId": account_id,
                        "contractId": contract_id,
                        "orderId": order_id,
                        "price": float(fill_price),
                        "side": int(side),
                        "size": float(size),
                        "profitAndLoss": None,
                        "creationTimestamp": now_ts,
                    }
                )

                sl_usd, tp_usd, tick_size, tick_value = self._get_bracket_settings(state, account_id)
                sl_ticks, tp_ticks = self._resolve_bracket_ticks(payload, state, account_id, tick_value)
                sl_offset = sl_ticks * tick_size
                tp_offset = tp_ticks * tick_size

                if int(side) == 0:
                    sl_price = _round_to_tick(float(fill_price) - sl_offset, tick_size)
                    tp_price = _round_to_tick(float(fill_price) + tp_offset, tick_size)
                    child_side = 1
                else:
                    sl_price = _round_to_tick(float(fill_price) + sl_offset, tick_size)
                    tp_price = _round_to_tick(float(fill_price) - tp_offset, tick_size)
                    child_side = 0

                sl_order_id = state["nextOrderId"]
                state["nextOrderId"] += 1
                tp_order_id = state["nextOrderId"]
                state["nextOrderId"] += 1

                sl_order = {
                    "id": sl_order_id,
                    "accountId": account_id,
                    "contractId": contract_id,
                    "type": 4,
                    "side": child_side,
                    "size": float(size),
                    "stopPrice": sl_price,
                    "status": 1,
                    "creationTimestamp": now_ts,
                    "parentOrderId": order_id,
                    "symbolId": symbol,
                }
                tp_order = {
                    "id": tp_order_id,
                    "accountId": account_id,
                    "contractId": contract_id,
                    "type": 1,
                    "side": child_side,
                    "size": float(size),
                    "limitPrice": tp_price,
                    "status": 1,
                    "creationTimestamp": now_ts,
                    "parentOrderId": order_id,
                    "symbolId": symbol,
                }
                state["orders"].extend([sl_order, tp_order])
                state["bracket_links"][str(order_id)] = [sl_order_id, tp_order_id]

            _save_state_atomic(self.state_path, state)

        return self._response_success({"orderId": order_id})

    def search_orders(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = payload.get("accountId")
        start_ts = payload.get("startTimestamp")
        end_ts = payload.get("endTimestamp")
        if account_id is None:
            return self._response_error("INVALID_ARGUMENT", "accountId is required")

        with _STATE_LOCK:
            state = _load_state(self.state_path)
        orders = [order for order in state.get("orders", []) if order.get("accountId") == account_id]
        orders = self._filter_by_time_range(orders, start_ts, end_ts, "creationTimestamp")
        return self._response_success({"orders": orders})

    def search_open_orders(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = payload.get("accountId")
        if account_id is None:
            return self._response_error("INVALID_ARGUMENT", "accountId is required")

        with _STATE_LOCK:
            state = _load_state(self.state_path)
        orders = [
            order
            for order in state.get("orders", [])
            if order.get("accountId") == account_id and int(order.get("status", 0)) == 1
        ]
        return self._response_success({"orders": orders})

    def cancel_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = payload.get("accountId")
        order_id = payload.get("orderId")
        if account_id is None or order_id is None:
            return self._response_error("INVALID_ARGUMENT", "accountId and orderId are required")

        now_ts = _now_iso_utc()
        cancelled = False
        with _STATE_LOCK:
            state = _load_state(self.state_path)
            for order in state.get("orders", []):
                if order.get("id") == order_id and order.get("accountId") == account_id:
                    if int(order.get("status", 0)) == 1:
                        order["status"] = 3
                        order["updateTimestamp"] = now_ts
                        cancelled = True
                    break
            _save_state_atomic(self.state_path, state)
        return self._response_success({"orderId": order_id, "cancelled": cancelled})

    def modify_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = payload.get("accountId")
        order_id = payload.get("orderId")
        size = payload.get("size")
        limit_price = payload.get("limitPrice")
        stop_price = payload.get("stopPrice")
        if account_id is None or order_id is None:
            return self._response_error("INVALID_ARGUMENT", "accountId and orderId are required")

        updates = {}
        if size is not None:
            size_value = float(size)
            if size_value <= 0:
                return self._response_error("INVALID_ARGUMENT", "size must be greater than 0")
            updates["size"] = size_value
        if limit_price is not None:
            updates["limitPrice"] = float(limit_price)
        if stop_price is not None:
            updates["stopPrice"] = float(stop_price)
        if not updates:
            return self._response_error("INVALID_ARGUMENT", "No fields to modify")

        now_ts = _now_iso_utc()
        modified = False
        with _STATE_LOCK:
            state = _load_state(self.state_path)
            for order in state.get("orders", []):
                if order.get("id") == order_id and order.get("accountId") == account_id:
                    if int(order.get("status", 0)) != 1:
                        break
                    order.update(updates)
                    order["updateTimestamp"] = now_ts
                    modified = True
                    break
            _save_state_atomic(self.state_path, state)

        if not modified:
            return self._response_error("ORDER_NOT_OPEN", "Order is not open")
        return self._response_success({"orderId": order_id})

    def search_open_positions(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = payload.get("accountId")
        if account_id is None:
            return self._response_error("INVALID_ARGUMENT", "accountId is required")

        with _STATE_LOCK:
            state = _load_state(self.state_path)
        positions = [
            position
            for position in state.get("positions", [])
            if position.get("accountId") == account_id and float(position.get("size", 0) or 0) != 0
        ]
        return self._response_success({"positions": positions})

    def close_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = payload.get("accountId")
        contract_id = payload.get("contractId")
        if account_id is None or contract_id is None:
            return self._response_error("INVALID_ARGUMENT", "accountId and contractId are required")

        symbol = symbol_from_contract(contract_id)
        price_feed = self._get_price_feed()
        exit_price, _ = price_feed.get_latest_close(symbol, timeframe_preference=["1m", "5m"])
        if exit_price is None:
            return self._response_error("NO_MARKET_DATA", "No market data available for close")

        now_ts = _now_iso_utc()
        with _STATE_LOCK:
            state = _load_state(self.state_path)
            state.setdefault("orders", [])
            state.setdefault("positions", [])
            state.setdefault("trades", [])
            state.setdefault("accounts", [])
            state.setdefault("nextOrderId", 1)
            state.setdefault("nextTradeId", 1)
            state.setdefault("bracket_links", {})

            position = None
            for existing in state["positions"]:
                if existing.get("accountId") == account_id and existing.get("contractId") == contract_id:
                    if float(existing.get("size", 0) or 0) != 0:
                        position = existing
                    break
            if position is None:
                return self._response_error("NOT_FOUND", "No open position found")

            size = float(position.get("size") or 0)
            position_type = int(position.get("type", 0))
            is_long = position_type == 1
            exit_side = 1 if is_long else 0

            tick_size = float(self.config.get("SIM_DEFAULT_TICK_SIZE", 0.25))
            tick_value = float(self.config.get("SIM_DEFAULT_TICK_VALUE", 1.25))
            entry_price = float(position.get("averagePrice") or 0.0)
            sign = 1 if is_long else -1
            pnl = ((exit_price - entry_price) / tick_size) * tick_value * size * sign if tick_size else 0.0

            order_id = state["nextOrderId"]
            state["nextOrderId"] += 1
            state["orders"].append(
                {
                    "id": order_id,
                    "accountId": account_id,
                    "contractId": contract_id,
                    "type": 2,
                    "side": exit_side,
                    "size": size,
                    "status": 2,
                    "fillVolume": size,
                    "filledPrice": float(exit_price),
                    "creationTimestamp": now_ts,
                    "updateTimestamp": now_ts,
                }
            )

            trade_id = state["nextTradeId"]
            state["nextTradeId"] += 1
            state["trades"].append(
                {
                    "id": trade_id,
                    "accountId": account_id,
                    "contractId": contract_id,
                    "orderId": order_id,
                    "price": float(exit_price),
                    "side": exit_side,
                    "size": size,
                    "profitAndLoss": pnl,
                    "creationTimestamp": now_ts,
                }
            )

            for account in state.get("accounts", []):
                if account.get("id") == account_id:
                    account["balance"] = float(account.get("balance", 0.0)) + pnl
                    break

            state["positions"].remove(position)
            self._cancel_open_children(state, account_id, contract_id, now_ts)
            _save_state_atomic(self.state_path, state)

        return self._response_success({"orderId": order_id, "tradeId": trade_id, "profitAndLoss": pnl})

    def partial_close_contract(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = payload.get("accountId")
        contract_id = payload.get("contractId")
        close_size = payload.get("size")
        if account_id is None or contract_id is None or close_size is None:
            return self._response_error("INVALID_ARGUMENT", "accountId, contractId, and size are required")
        close_size = float(close_size)
        if close_size <= 0:
            return self._response_error("INVALID_ARGUMENT", "size must be greater than 0")

        symbol = symbol_from_contract(contract_id)
        price_feed = self._get_price_feed()
        exit_price, _ = price_feed.get_latest_close(symbol, timeframe_preference=["1m", "5m"])
        if exit_price is None:
            return self._response_error("NO_MARKET_DATA", "No market data available for close")

        now_ts = _now_iso_utc()
        with _STATE_LOCK:
            state = _load_state(self.state_path)
            state.setdefault("orders", [])
            state.setdefault("positions", [])
            state.setdefault("trades", [])
            state.setdefault("accounts", [])
            state.setdefault("nextOrderId", 1)
            state.setdefault("nextTradeId", 1)
            state.setdefault("bracket_links", {})

            position = None
            for existing in state["positions"]:
                if existing.get("accountId") == account_id and existing.get("contractId") == contract_id:
                    if float(existing.get("size", 0) or 0) != 0:
                        position = existing
                    break
            if position is None:
                return self._response_error("NOT_FOUND", "No open position found")

            current_size = float(position.get("size") or 0)
            if close_size > current_size:
                return self._response_error("INVALID_ARGUMENT", "size exceeds open position size")

            position_type = int(position.get("type", 0))
            is_long = position_type == 1
            exit_side = 1 if is_long else 0

            tick_size = float(self.config.get("SIM_DEFAULT_TICK_SIZE", 0.25))
            tick_value = float(self.config.get("SIM_DEFAULT_TICK_VALUE", 1.25))
            entry_price = float(position.get("averagePrice") or 0.0)
            sign = 1 if is_long else -1
            pnl = ((exit_price - entry_price) / tick_size) * tick_value * close_size * sign if tick_size else 0.0

            order_id = state["nextOrderId"]
            state["nextOrderId"] += 1
            state["orders"].append(
                {
                    "id": order_id,
                    "accountId": account_id,
                    "contractId": contract_id,
                    "type": 2,
                    "side": exit_side,
                    "size": close_size,
                    "status": 2,
                    "fillVolume": close_size,
                    "filledPrice": float(exit_price),
                    "creationTimestamp": now_ts,
                    "updateTimestamp": now_ts,
                }
            )

            trade_id = state["nextTradeId"]
            state["nextTradeId"] += 1
            state["trades"].append(
                {
                    "id": trade_id,
                    "accountId": account_id,
                    "contractId": contract_id,
                    "orderId": order_id,
                    "price": float(exit_price),
                    "side": exit_side,
                    "size": close_size,
                    "profitAndLoss": pnl,
                    "creationTimestamp": now_ts,
                }
            )

            for account in state.get("accounts", []):
                if account.get("id") == account_id:
                    account["balance"] = float(account.get("balance", 0.0)) + pnl
                    break

            remaining = current_size - close_size
            if remaining <= 0:
                state["positions"].remove(position)
                self._cancel_open_children(state, account_id, contract_id, now_ts)
            else:
                position["size"] = remaining
                self._resize_open_children(state, account_id, contract_id, remaining)

            _save_state_atomic(self.state_path, state)

        return self._response_success(
            {
                "orderId": order_id,
                "tradeId": trade_id,
                "profitAndLoss": pnl,
                "remainingSize": max(0.0, remaining),
            }
        )

    def search_trades(self, payload: dict[str, Any]) -> dict[str, Any]:
        account_id = payload.get("accountId")
        start_ts = payload.get("startTimestamp")
        end_ts = payload.get("endTimestamp")
        if account_id is None:
            return self._response_error("INVALID_ARGUMENT", "accountId is required")

        with _STATE_LOCK:
            state = _load_state(self.state_path)

        trades = [t for t in state.get("trades", []) if t.get("accountId") == account_id]
        trades = self._filter_by_time_range(trades, start_ts, end_ts, "creationTimestamp")
        return self._response_success({"trades": trades})

    def _cancel_open_children(self, state: dict[str, Any], account_id: int, contract_id: str, now_ts: str) -> None:
        orders = state.get("orders", [])
        open_child_ids = []
        for order in orders:
            if (
                order.get("accountId") == account_id
                and order.get("contractId") == contract_id
                and order.get("parentOrderId") is not None
                and int(order.get("status", 0)) == 1
            ):
                order["status"] = 3
                order["updateTimestamp"] = now_ts
                open_child_ids.append(order.get("id"))
        if not open_child_ids:
            return
        bracket_links = state.get("bracket_links", {})
        to_remove = []
        for parent_id, child_ids in bracket_links.items():
            if any(child_id in open_child_ids for child_id in child_ids):
                to_remove.append(parent_id)
        for parent_id in to_remove:
            bracket_links.pop(parent_id, None)

    def _resize_open_children(self, state: dict[str, Any], account_id: int, contract_id: str, size: float) -> None:
        for order in state.get("orders", []):
            if (
                order.get("accountId") == account_id
                and order.get("contractId") == contract_id
                and order.get("parentOrderId") is not None
                and int(order.get("status", 0)) == 1
            ):
                order["size"] = size
