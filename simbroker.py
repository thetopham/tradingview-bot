import csv
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import load_config

_STATE_LOCK = threading.Lock()
_SIM_BROKER = None

ORDER_SIDE_BID = 0
ORDER_SIDE_ASK = 1

ORDER_TYPE_LIMIT = 1
ORDER_TYPE_MARKET = 2
ORDER_TYPE_STOP = 4

ORDER_STATUS_OPEN = 1
ORDER_STATUS_FILLED = 2
ORDER_STATUS_CANCELLED = 3
ORDER_STATUS_EXPIRED = 4
ORDER_STATUS_REJECTED = 5
ORDER_STATUS_PENDING = 6

POSITION_TYPE_LONG = 1
POSITION_TYPE_SHORT = 2


def get_sim_broker():
    global _SIM_BROKER
    if _SIM_BROKER is None:
        _SIM_BROKER = SimBroker()
    return _SIM_BROKER


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), timezone.utc)
    value = str(ts).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return round(price / tick_size) * tick_size


class SimBroker:
    """Drop-in ProjectX Gateway sim broker.

    This mirrors the REST API response shapes defined in the ProjectX Gateway
    contract. It persists state to a JSON file and replays fills off local CSV
    bars while exposing bracket child orders in searchOpen.
    """

    def __init__(self, *, state_path: Optional[str] = None, data_paths: Optional[List[str]] = None, config: Optional[Dict] = None):
        self.config = config or load_config()
        self.state_path = state_path or self.config.get("SIM_STATE_PATH")
        self.data_paths = data_paths or self._resolve_data_paths()
        self.tick_size = float(self.config.get("SIM_TICK_SIZE", 0.25) or 0.25)
        self.tick_value = float(self.config.get("SIM_TICK_VALUE", 1.25) or 1.25)
        self.bracket_sl_usd = float(self.config.get("SIM_BRACKET_SL_USD", 30.0) or 30.0)
        self.bracket_tp_usd = float(self.config.get("SIM_BRACKET_TP_USD", 60.0) or 60.0)
        self.fill_policy = str(self.config.get("SIM_FILL_POLICY", "worst") or "worst").lower()
        self.start_balance = float(self.config.get("SIM_START_BALANCE", 50000.0) or 50000.0)
        self._bars_cache: Dict[str, List[Dict]] = {}
        self.state = self._load_state()
        self._ensure_accounts()

    def handle_request(self, path: str, payload: Dict) -> Dict:
        normalized = path.lower().strip()
        if not normalized.startswith("/api/"):
            normalized = f"/api/{normalized.lstrip('/')}"

        account_id = payload.get("accountId") if isinstance(payload, dict) else None
        contract_id = payload.get("contractId") if isinstance(payload, dict) else None
        now_ts = payload.get("nowTimestamp") or payload.get("now_ts_iso")

        if not normalized.startswith("/api/auth") and normalized != "/api/order/searchopen":
            self._maybe_sim_update(account_id, contract_id, now_ts)

        if normalized == "/api/auth/loginkey":
            return {
                "token": "SIM_TOKEN",
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }
        if normalized == "/api/auth/validate":
            return {
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
                "newToken": "SIM_TOKEN_REFRESH",
            }
        if normalized == "/api/account/search":
            return self._account_search()
        if normalized == "/api/contract/available":
            return self._contract_available(payload)
        if normalized == "/api/contract/search":
            return self._contract_search(payload)
        if normalized == "/api/contract/searchbyid":
            return self._contract_search_by_id(payload)
        if normalized == "/api/order/place":
            return self._order_place(payload)
        if normalized == "/api/order/search":
            return self._order_search(payload)
        if normalized == "/api/order/searchopen":
            return self._order_search_open(payload)
        if normalized == "/api/order/cancel":
            return self._order_cancel(payload)
        if normalized == "/api/order/modify":
            return self._order_modify(payload)
        if normalized == "/api/position/searchopen":
            return self._position_search_open(payload)
        if normalized == "/api/position/closecontract":
            return self._position_close_contract(payload)
        if normalized == "/api/position/partialclosecontract":
            return self._position_partial_close(payload)
        if normalized == "/api/trade/search":
            return self._trade_search(payload)
        if normalized == "/api/history/retrievebars":
            return self._history_retrieve_bars(payload)

        return {
            "success": False,
            "errorCode": 404,
            "errorMessage": f"Unknown sim endpoint: {path}",
        }

    def sim_update(self, account_id: int, contract_id: str, now_ts_iso: Optional[str] = None) -> None:
        bars = self._load_bars_for_unit(unit=2, unit_number=5)
        if not bars:
            return

        now_ts = _parse_iso(now_ts_iso) if now_ts_iso else bars[-1]["t"]
        if not now_ts:
            return

        last_processed = self._get_last_processed_ts(account_id, contract_id)
        for bar in bars:
            bar_ts = bar["t"]
            if last_processed and bar_ts <= last_processed:
                continue
            if bar_ts > now_ts:
                break

            position = self._get_open_position(account_id, contract_id)
            if position:
                self._process_brackets_for_bar(position, bar)

            last_processed = bar_ts

        if last_processed:
            self._set_last_processed_ts(account_id, contract_id, last_processed)
            self._persist_state()

    def _maybe_sim_update(self, account_id: Optional[int], contract_id: Optional[str], now_ts_iso: Optional[str]) -> None:
        if not account_id:
            return
        if contract_id:
            self.sim_update(int(account_id), contract_id, now_ts_iso)
            return
        for position in self._open_positions_for_account(int(account_id)):
            self.sim_update(int(account_id), position["contractId"], now_ts_iso)

    def _order_place(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        contract_id = payload.get("contractId")
        order_type = int(payload.get("type"))
        side = int(payload.get("side"))
        size = int(payload.get("size", 0))
        limit_price = payload.get("limitPrice")
        stop_price = payload.get("stopPrice")
        custom_tag = payload.get("customTag")

        if size <= 0:
            return self._error("Order size must be positive")

        now = _utc_now_iso()
        order_id = self._next_id("nextOrderId")
        order = {
            "id": order_id,
            "accountId": account_id,
            "contractId": contract_id,
            "symbolId": self._symbol_id_for_contract(contract_id),
            "creationTimestamp": now,
            "updateTimestamp": now,
            "status": ORDER_STATUS_OPEN,
            "type": order_type,
            "side": side,
            "size": size,
            "limitPrice": limit_price,
            "stopPrice": stop_price,
            "filledPrice": None,
            "fillVolume": 0,
            "customTag": custom_tag,
        }

        if order_type == ORDER_TYPE_MARKET:
            fill_price = self._current_price()
            order.update(
                {
                    "status": ORDER_STATUS_FILLED,
                    "filledPrice": fill_price,
                    "fillVolume": size,
                    "updateTimestamp": _utc_now_iso(),
                }
            )
            self.state["orders"].append(order)
            self._apply_fill(account_id, contract_id, side, size, fill_price, order_id)
            self._create_or_update_brackets(account_id, contract_id, order_id)
            self._persist_state()
            return {
                "orderId": order_id,
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        self.state["orders"].append(order)
        self._persist_state()
        return {
            "orderId": order_id,
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
        }

    def _order_search_open(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        orders = [
            o
            for o in self.state["orders"]
            if o.get("accountId") == account_id and o.get("status") == ORDER_STATUS_OPEN
        ]
        return {
            "orders": orders,
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
        }

    def _order_search(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        start_ts = _parse_iso(payload.get("startTimestamp"))
        end_ts = _parse_iso(payload.get("endTimestamp"))

        def in_window(order: Dict) -> bool:
            ts = _parse_iso(order.get("creationTimestamp"))
            if not ts:
                return False
            if start_ts and ts < start_ts:
                return False
            if end_ts and ts > end_ts:
                return False
            return True

        orders = [
            o
            for o in self.state["orders"]
            if o.get("accountId") == account_id and in_window(o)
        ]
        return {
            "orders": orders,
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
        }

    def _order_cancel(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        order_id = int(payload.get("orderId"))
        order = self._find_order(order_id, account_id)
        if not order or order.get("status") != ORDER_STATUS_OPEN:
            return self._error("Order not open", error_code=404)
        order["status"] = ORDER_STATUS_CANCELLED
        order["updateTimestamp"] = _utc_now_iso()
        self._persist_state()
        return {"success": True, "errorCode": 0, "errorMessage": None}

    def _order_modify(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        order_id = int(payload.get("orderId"))
        order = self._find_order(order_id, account_id)
        if not order or order.get("status") != ORDER_STATUS_OPEN:
            return self._error("Order not open", error_code=404)

        for key in ("size", "limitPrice", "stopPrice"):
            if key in payload:
                order[key] = payload[key]
        order["updateTimestamp"] = _utc_now_iso()
        self._persist_state()
        return {"success": True, "errorCode": 0, "errorMessage": None}

    def _position_search_open(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        positions = [p for p in self.state["positions"] if p.get("accountId") == account_id and p.get("isOpen")]
        return {
            "positions": positions,
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
        }

    def _position_close_contract(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        contract_id = payload.get("contractId")
        position = self._get_open_position(account_id, contract_id)
        if not position:
            return self._error("Position not found", error_code=404)
        price = self._current_price()
        self._close_position(position, price, reason="manual_close")
        self._persist_state()
        return {"success": True, "errorCode": 0, "errorMessage": None}

    def _position_partial_close(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        contract_id = payload.get("contractId")
        size = int(payload.get("size", 0))
        if size <= 0:
            return self._error("Size must be positive")
        position = self._get_open_position(account_id, contract_id)
        if not position:
            return self._error("Position not found", error_code=404)
        price = self._current_price()
        self._reduce_position(position, size, price)
        self._persist_state()
        return {"success": True, "errorCode": 0, "errorMessage": None}

    def _trade_search(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        start_ts = _parse_iso(payload.get("startTimestamp"))
        end_ts = _parse_iso(payload.get("endTimestamp"))

        def in_window(trade: Dict) -> bool:
            ts = _parse_iso(trade.get("creationTimestamp"))
            if not ts:
                return False
            if start_ts and ts < start_ts:
                return False
            if end_ts and ts > end_ts:
                return False
            return True

        trades = [
            t
            for t in self.state["trades"]
            if t.get("accountId") == account_id and in_window(t)
        ]
        return {
            "trades": trades,
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
        }

    def _account_search(self) -> Dict:
        accounts = list(self.state["accounts"].values())
        return {
            "accounts": accounts,
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
        }

    def _contract_available(self, payload: Dict) -> Dict:
        contract_id = payload.get("contractId") or self.config.get("OVERRIDE_CONTRACT_ID")
        return {
            "contracts": [self._contract_for_id(contract_id)],
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
        }

    def _contract_search(self, payload: Dict) -> Dict:
        search_text = str(payload.get("searchText") or "").upper()
        contract_id = self.config.get("OVERRIDE_CONTRACT_ID")
        contract = self._contract_for_id(contract_id)
        if search_text and search_text not in contract["id"] and search_text not in contract.get("name", ""):
            contracts = []
        else:
            contracts = [contract]
        return {
            "contracts": contracts,
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
        }

    def _contract_search_by_id(self, payload: Dict) -> Dict:
        contract_id = payload.get("contractId") or self.config.get("OVERRIDE_CONTRACT_ID")
        return {
            "contracts": [self._contract_for_id(contract_id)],
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
        }

    def _history_retrieve_bars(self, payload: Dict) -> Dict:
        unit = int(payload.get("unit", 2))
        unit_number = int(payload.get("unitNumber", 5))
        start_ts = _parse_iso(payload.get("startTime"))
        end_ts = _parse_iso(payload.get("endTime"))
        limit = payload.get("limit")

        bars = self._load_bars_for_unit(unit=unit, unit_number=unit_number)
        if not bars:
            return {"bars": [], "success": True, "errorCode": 0, "errorMessage": None}

        filtered = []
        for bar in bars:
            ts = bar["t"]
            if start_ts and ts < start_ts:
                continue
            if end_ts and ts > end_ts:
                continue
            filtered.append(bar)

        if isinstance(limit, int) and limit > 0:
            filtered = filtered[:limit]

        return {
            "bars": [
                {
                    "t": b["t"].isoformat(),
                    "o": b["o"],
                    "h": b["h"],
                    "l": b["l"],
                    "c": b["c"],
                    "v": b.get("v", 0),
                }
                for b in filtered
            ],
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
        }

    def _apply_fill(self, account_id: int, contract_id: str, side: int, size: int, price: float, order_id: int) -> None:
        position = self._get_open_position(account_id, contract_id)
        order_side = ORDER_SIDE_BID if side == ORDER_SIDE_BID else ORDER_SIDE_ASK
        if not position:
            position_type = POSITION_TYPE_LONG if order_side == ORDER_SIDE_BID else POSITION_TYPE_SHORT
            position_id = self._next_id("nextPositionId")
            position = {
                "id": position_id,
                "accountId": account_id,
                "contractId": contract_id,
                "creationTimestamp": _utc_now_iso(),
                "type": position_type,
                "size": size,
                "averagePrice": price,
                "isOpen": True,
                "updateTimestamp": _utc_now_iso(),
            }
            self.state["positions"].append(position)
            self._record_trade(account_id, contract_id, price, 0.0, side, size, order_id)
            self._set_last_processed_ts(account_id, contract_id, self._previous_bar_ts())
            return

        same_side = (
            position["type"] == POSITION_TYPE_LONG and order_side == ORDER_SIDE_BID
        ) or (
            position["type"] == POSITION_TYPE_SHORT and order_side == ORDER_SIDE_ASK
        )

        if same_side:
            new_size = position["size"] + size
            position["averagePrice"] = (
                (position["averagePrice"] * position["size"]) + (price * size)
            ) / new_size
            position["size"] = new_size
            position["updateTimestamp"] = _utc_now_iso()
            self._record_trade(account_id, contract_id, price, 0.0, side, size, order_id)
            self._set_last_processed_ts(account_id, contract_id, self._previous_bar_ts())
            return

        close_size = min(position["size"], size)
        pnl = self._compute_pnl(position, price, close_size)
        self._record_trade(account_id, contract_id, price, pnl, side, close_size, order_id)
        self._apply_balance(account_id, pnl)
        position["size"] -= close_size
        position["updateTimestamp"] = _utc_now_iso()

        remaining = size - close_size
        if position["size"] <= 0:
            position["isOpen"] = False
            position["closedTimestamp"] = _utc_now_iso()
            self._cancel_brackets(account_id, contract_id)

        if remaining > 0:
            position_type = POSITION_TYPE_LONG if order_side == ORDER_SIDE_BID else POSITION_TYPE_SHORT
            new_position = {
                "id": self._next_id("nextPositionId"),
                "accountId": account_id,
                "contractId": contract_id,
                "creationTimestamp": _utc_now_iso(),
                "type": position_type,
                "size": remaining,
                "averagePrice": price,
                "isOpen": True,
                "updateTimestamp": _utc_now_iso(),
            }
            self.state["positions"].append(new_position)
            self._record_trade(account_id, contract_id, price, 0.0, side, remaining, order_id)

        self._set_last_processed_ts(account_id, contract_id, self._previous_bar_ts())

    def _reduce_position(self, position: Dict, size: int, price: float) -> None:
        close_size = min(position["size"], size)
        side = ORDER_SIDE_ASK if position["type"] == POSITION_TYPE_LONG else ORDER_SIDE_BID
        pnl = self._compute_pnl(position, price, close_size)
        self._record_trade(position["accountId"], position["contractId"], price, pnl, side, close_size, None)
        self._apply_balance(position["accountId"], pnl)
        position["size"] -= close_size
        position["updateTimestamp"] = _utc_now_iso()
        if position["size"] <= 0:
            position["isOpen"] = False
            position["closedTimestamp"] = _utc_now_iso()
            self._cancel_brackets(position["accountId"], position["contractId"])
        else:
            self._resize_brackets(position)

    def _close_position(self, position: Dict, price: float, reason: str) -> None:
        close_size = position["size"]
        side = ORDER_SIDE_ASK if position["type"] == POSITION_TYPE_LONG else ORDER_SIDE_BID
        pnl = self._compute_pnl(position, price, close_size)
        self._record_trade(position["accountId"], position["contractId"], price, pnl, side, close_size, None)
        self._apply_balance(position["accountId"], pnl)
        position["size"] = 0
        position["isOpen"] = False
        position["closedTimestamp"] = _utc_now_iso()
        position["updateTimestamp"] = _utc_now_iso()
        self._cancel_brackets(position["accountId"], position["contractId"])

    def _process_brackets_for_bar(self, position: Dict, bar: Dict) -> None:
        bracket = self._get_bracket(position["accountId"], position["contractId"])
        if not bracket:
            return

        tp_price = bracket.get("tp_price")
        sl_price = bracket.get("sl_price")
        if tp_price is None or sl_price is None:
            return

        high = bar["h"]
        low = bar["l"]

        tp_hit = False
        sl_hit = False
        if position["type"] == POSITION_TYPE_LONG:
            tp_hit = high >= tp_price
            sl_hit = low <= sl_price
        else:
            tp_hit = low <= tp_price
            sl_hit = high >= sl_price

        if not tp_hit and not sl_hit:
            return

        trigger = None
        if tp_hit and sl_hit:
            trigger = "tp" if self.fill_policy == "best" else "sl"
        elif tp_hit:
            trigger = "tp"
        else:
            trigger = "sl"

        if trigger == "tp":
            self._fill_bracket_order(bracket.get("tp_order_id"), filled=True)
            self._fill_bracket_order(bracket.get("sl_order_id"), filled=False)
            exit_price = tp_price
        else:
            self._fill_bracket_order(bracket.get("sl_order_id"), filled=True)
            self._fill_bracket_order(bracket.get("tp_order_id"), filled=False)
            exit_price = sl_price

        self._close_position(position, exit_price, reason="bracket")

    def _fill_bracket_order(self, order_id: Optional[int], filled: bool) -> None:
        if not order_id:
            return
        order = self._find_order(order_id, None)
        if not order:
            return
        order["status"] = ORDER_STATUS_FILLED if filled else ORDER_STATUS_CANCELLED
        order["updateTimestamp"] = _utc_now_iso()

    def _create_or_update_brackets(self, account_id: int, contract_id: str, parent_order_id: int) -> None:
        position = self._get_open_position(account_id, contract_id)
        if not position:
            return
        self._cancel_brackets(account_id, contract_id)

        entry_price = position["averagePrice"]
        sl_price, tp_price = self._compute_bracket_prices(position["type"], entry_price)

        side = ORDER_SIDE_ASK if position["type"] == POSITION_TYPE_LONG else ORDER_SIDE_BID
        now = _utc_now_iso()
        sl_order_id = self._next_id("nextOrderId")
        tp_order_id = self._next_id("nextOrderId")

        sl_order = {
            "id": sl_order_id,
            "accountId": account_id,
            "contractId": contract_id,
            "symbolId": self._symbol_id_for_contract(contract_id),
            "creationTimestamp": now,
            "updateTimestamp": now,
            "status": ORDER_STATUS_OPEN,
            "type": ORDER_TYPE_STOP,
            "side": side,
            "size": position["size"],
            "limitPrice": None,
            "stopPrice": sl_price,
            "filledPrice": None,
            "fillVolume": 0,
            "customTag": "SIM_BRACKET_SL",
            "parentOrderId": parent_order_id,
        }
        tp_order = {
            "id": tp_order_id,
            "accountId": account_id,
            "contractId": contract_id,
            "symbolId": self._symbol_id_for_contract(contract_id),
            "creationTimestamp": now,
            "updateTimestamp": now,
            "status": ORDER_STATUS_OPEN,
            "type": ORDER_TYPE_LIMIT,
            "side": side,
            "size": position["size"],
            "limitPrice": tp_price,
            "stopPrice": None,
            "filledPrice": None,
            "fillVolume": 0,
            "customTag": "SIM_BRACKET_TP",
            "parentOrderId": parent_order_id,
        }

        self.state["orders"].extend([sl_order, tp_order])
        self.state["brackets"][self._bracket_key(account_id, contract_id)] = {
            "parent_order_id": parent_order_id,
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
            "sl_price": sl_price,
            "tp_price": tp_price,
            "size": position["size"],
        }

    def _resize_brackets(self, position: Dict) -> None:
        bracket = self._get_bracket(position["accountId"], position["contractId"])
        if not bracket:
            return
        for order_id in (bracket.get("sl_order_id"), bracket.get("tp_order_id")):
            order = self._find_order(order_id, position["accountId"])
            if order and order.get("status") == ORDER_STATUS_OPEN:
                order["size"] = position["size"]
                order["updateTimestamp"] = _utc_now_iso()
        bracket["size"] = position["size"]

    def _cancel_brackets(self, account_id: int, contract_id: str) -> None:
        bracket = self._get_bracket(account_id, contract_id)
        if not bracket:
            return
        for order_id in (bracket.get("sl_order_id"), bracket.get("tp_order_id")):
            order = self._find_order(order_id, account_id)
            if order and order.get("status") == ORDER_STATUS_OPEN:
                order["status"] = ORDER_STATUS_CANCELLED
                order["updateTimestamp"] = _utc_now_iso()
        self.state["brackets"].pop(self._bracket_key(account_id, contract_id), None)

    def _compute_bracket_prices(self, position_type: int, entry_price: float) -> tuple[float, float]:
        sl_ticks = round(self.bracket_sl_usd / self.tick_value)
        tp_ticks = round(self.bracket_tp_usd / self.tick_value)
        sl_offset = sl_ticks * self.tick_size
        tp_offset = tp_ticks * self.tick_size

        if position_type == POSITION_TYPE_LONG:
            sl = entry_price - sl_offset
            tp = entry_price + tp_offset
        else:
            sl = entry_price + sl_offset
            tp = entry_price - tp_offset

        return _round_to_tick(sl, self.tick_size), _round_to_tick(tp, self.tick_size)

    def _compute_pnl(self, position: Dict, exit_price: float, size: int) -> float:
        entry_price = position.get("averagePrice") or 0.0
        price_diff = exit_price - entry_price
        ticks = price_diff / self.tick_size if self.tick_size else 0
        pnl_per_contract = ticks * self.tick_value
        if position["type"] == POSITION_TYPE_SHORT:
            pnl_per_contract *= -1
        return pnl_per_contract * size

    def _record_trade(
        self,
        account_id: int,
        contract_id: str,
        price: float,
        pnl: float,
        side: int,
        size: int,
        order_id: Optional[int],
    ) -> None:
        trade_id = self._next_id("nextTradeId")
        trade = {
            "id": trade_id,
            "accountId": account_id,
            "contractId": contract_id,
            "creationTimestamp": _utc_now_iso(),
            "price": price,
            "profitAndLoss": pnl,
            "fees": 0.0,
            "side": side,
            "size": size,
            "voided": False,
            "orderId": order_id,
        }
        self.state["trades"].append(trade)

    def _apply_balance(self, account_id: int, pnl: float) -> None:
        acct = self.state["accounts"].get(str(account_id))
        if not acct:
            return
        acct["balance"] = float(acct.get("balance", 0.0)) + float(pnl)

    def _current_price(self) -> float:
        bars = self._load_bars_for_unit(unit=2, unit_number=5)
        if not bars:
            return 0.0
        return float(bars[-1]["c"])

    def _previous_bar_ts(self) -> Optional[datetime]:
        bars = self._load_bars_for_unit(unit=2, unit_number=5)
        if len(bars) >= 2:
            return bars[-2]["t"]
        if bars:
            return bars[-1]["t"]
        return None

    def _load_state(self) -> Dict:
        if not self.state_path:
            raise RuntimeError("SIM_STATE_PATH is required")
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with _STATE_LOCK:
            if os.path.exists(self.state_path):
                try:
                    with open(self.state_path, "r", encoding="utf-8") as handle:
                        return json.load(handle)
                except Exception as exc:
                    logging.warning("Failed to load sim state: %s", exc)
        return {
            "nextOrderId": 1,
            "nextPositionId": 1,
            "nextTradeId": 1,
            "accounts": {},
            "orders": [],
            "positions": [],
            "trades": [],
            "brackets": {},
            "last_processed_bar_ts": {},
        }

    def _persist_state(self) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp_path = f"{self.state_path}.tmp"
        with _STATE_LOCK:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.state_path)

    def _ensure_accounts(self) -> None:
        accounts = self.config.get("ACCOUNTS", {})
        for name, account_id in accounts.items():
            key = str(account_id)
            if key not in self.state["accounts"]:
                self.state["accounts"][key] = {
                    "id": int(account_id),
                    "name": str(name).upper(),
                    "balance": self.start_balance,
                    "canTrade": True,
                    "isVisible": True,
                    "simulated": True,
                }
        self._persist_state()

    def _resolve_data_paths(self) -> List[str]:
        env_paths = self.config.get("SIM_DATA_PATHS") or os.getenv("SIM_DATA_PATHS")
        if env_paths:
            return [p.strip() for p in str(env_paths).split(",") if p.strip()]
        return [
            "/mnt/data/tv_datafeed_5m_rows (2).csv",
            "/mnt/data/tv_datafeed_5m_rows.csv",
            "/mnt/data/tv_datafeed_15m_rows.csv",
            "/mnt/data/tv_datafeed_30m_rows.csv",
            "/mnt/data/tv_datafeed_1d_rows.csv",
        ]

    def _load_bars_for_unit(self, *, unit: int, unit_number: int) -> List[Dict]:
        path = self._select_data_path(unit, unit_number)
        if not path:
            return []
        if path in self._bars_cache:
            return self._bars_cache[path]

        bars: List[Dict] = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    bar = self._parse_bar_row(row)
                    if bar:
                        bars.append(bar)
        except FileNotFoundError:
            logging.warning("Sim broker datafeed not found: %s", path)
        except Exception as exc:
            logging.warning("Failed to load sim bars from %s: %s", path, exc)

        bars.sort(key=lambda b: b["t"])
        self._bars_cache[path] = bars
        return bars

    def _select_data_path(self, unit: int, unit_number: int) -> Optional[str]:
        preferred = None
        if unit == 2 and unit_number == 5:
            preferred = "5m"
        elif unit == 2 and unit_number == 15:
            preferred = "15m"
        elif unit == 2 and unit_number == 30:
            preferred = "30m"
        elif unit == 4:
            preferred = "1d"

        if preferred:
            for path in self.data_paths:
                if preferred in os.path.basename(path).lower() and os.path.exists(path):
                    return path

        for path in self.data_paths:
            if os.path.exists(path):
                return path
        return None

    def _parse_bar_row(self, row: Dict) -> Optional[Dict]:
        if not row:
            return None

        def _value(*keys):
            for key in keys:
                if key in row and row[key] not in (None, ""):
                    return row[key]
            return None

        ts_raw = _value("t", "time", "timestamp", "ts", "date")
        if ts_raw is None:
            return None

        ts = None
        if isinstance(ts_raw, (int, float)):
            ts = datetime.fromtimestamp(float(ts_raw), timezone.utc)
        else:
            raw = str(ts_raw).strip()
            if raw.isdigit():
                ts_val = int(raw)
                ts = datetime.fromtimestamp(ts_val / 1000 if ts_val > 1_000_000_000_000 else ts_val, timezone.utc)
            else:
                if raw.endswith("Z"):
                    raw = raw[:-1] + "+00:00"
                try:
                    parsed = datetime.fromisoformat(raw)
                    ts = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                except ValueError:
                    return None

        try:
            o = float(_value("o", "open"))
            h = float(_value("h", "high"))
            l = float(_value("l", "low"))
            c = float(_value("c", "close"))
        except (TypeError, ValueError):
            return None

        volume = _value("v", "volume")
        try:
            v = int(float(volume)) if volume is not None else 0
        except (TypeError, ValueError):
            v = 0

        return {"t": ts, "o": o, "h": h, "l": l, "c": c, "v": v}

    def _symbol_id_for_contract(self, contract_id: str) -> str:
        parts = str(contract_id).split(".")
        if len(parts) >= 3:
            return ".".join(parts[1:-1])
        return str(contract_id)

    def _contract_for_id(self, contract_id: str) -> Dict:
        contract_id = contract_id or "CON.F.US.MES.H26"
        tick_size = self.tick_size
        tick_value = self.tick_value
        if "MES" in contract_id:
            tick_size = float(self.config.get("SIM_TICK_SIZE", 0.25) or 0.25)
            tick_value = float(self.config.get("SIM_TICK_VALUE", 1.25) or 1.25)
        return {
            "id": contract_id,
            "name": contract_id.split(".")[-1],
            "description": f"Simulated contract {contract_id}",
            "tickSize": tick_size,
            "tickValue": tick_value,
            "activeContract": True,
            "symbolId": self._symbol_id_for_contract(contract_id),
        }

    def _open_positions_for_account(self, account_id: int) -> List[Dict]:
        return [p for p in self.state["positions"] if p.get("accountId") == account_id and p.get("isOpen")]

    def _get_open_position(self, account_id: int, contract_id: str) -> Optional[Dict]:
        for pos in self.state["positions"]:
            if pos.get("accountId") == account_id and pos.get("contractId") == contract_id and pos.get("isOpen"):
                return pos
        return None

    def _get_last_processed_ts(self, account_id: int, contract_id: str) -> Optional[datetime]:
        account_key = str(account_id)
        contract_map = self.state.get("last_processed_bar_ts", {}).get(account_key, {})
        return _parse_iso(contract_map.get(contract_id))

    def _set_last_processed_ts(self, account_id: int, contract_id: str, ts: Optional[datetime]) -> None:
        if not ts:
            return
        account_key = str(account_id)
        self.state.setdefault("last_processed_bar_ts", {}).setdefault(account_key, {})[contract_id] = ts.isoformat()

    def _bracket_key(self, account_id: int, contract_id: str) -> str:
        return f"{account_id}:{contract_id}"

    def _get_bracket(self, account_id: int, contract_id: str) -> Optional[Dict]:
        return self.state.get("brackets", {}).get(self._bracket_key(account_id, contract_id))

    def _find_order(self, order_id: Optional[int], account_id: Optional[int]) -> Optional[Dict]:
        if order_id is None:
            return None
        for order in self.state["orders"]:
            if order.get("id") == order_id and (account_id is None or order.get("accountId") == account_id):
                return order
        return None

    def _next_id(self, key: str) -> int:
        current = int(self.state.get(key, 1))
        self.state[key] = current + 1
        return current

    def _error(self, message: str, error_code: int = 400) -> Dict:
        return {
            "success": False,
            "errorCode": error_code,
            "errorMessage": message,
        }
