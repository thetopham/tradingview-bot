import csv
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import load_config

STATE_LOCK = threading.RLock()

ORDER_SIDE_BID = 0
ORDER_SIDE_ASK = 1

ORDER_TYPE_LIMIT = 1
ORDER_TYPE_MARKET = 2
ORDER_TYPE_STOP = 4

ORDER_STATUS_OPEN = 1
ORDER_STATUS_FILLED = 2
ORDER_STATUS_CANCELLED = 3
ORDER_STATUS_REJECTED = 5

POSITION_TYPE_LONG = 1
POSITION_TYPE_SHORT = 2


class SimBroker:
    """SimBroker emulates ProjectX Gateway REST API responses with persisted state."""

    def __init__(self, state_path: Optional[str] = None):
        self._config = load_config()
        self._state_path = state_path or os.getenv(
            "SIMBROKER_STATE_PATH", "/mnt/data/simbroker_state.json"
        )
        self._bars_cache: Dict[str, Dict[str, Any]] = {}
        self._state = self._load_state()
        self._ensure_accounts()

    def handle_request(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = payload.get("accountId")
        contract_id = payload.get("contractId")
        if account_id and contract_id:
            self.sim_update(account_id, contract_id)

        if path == "/api/Auth/loginKey":
            return self._success({"token": "SIM_TOKEN", "newToken": "SIM_TOKEN"})
        if path == "/api/Auth/validate":
            return self._success({"newToken": "SIM_TOKEN"})
        if path == "/api/Account/search":
            return self._success({"accounts": list(self._state["accounts"].values())})
        if path in {"/api/Contract/available", "/api/Contract/search"}:
            contract_id = payload.get("contractId") or self._config.get("OVERRIDE_CONTRACT_ID")
            return self._success({"contracts": [self._build_contract(contract_id)]})
        if path == "/api/Contract/searchById":
            contract_id = payload.get("contractId") or self._config.get("OVERRIDE_CONTRACT_ID")
            return self._success({"contract": self._build_contract(contract_id)})
        if path == "/api/Order/place":
            return self._place_order(payload)
        if path == "/api/Order/search":
            return self._search_orders(payload)
        if path == "/api/Order/searchOpen":
            return self._search_open_orders(payload)
        if path == "/api/Order/cancel":
            return self._cancel_order(payload)
        if path == "/api/Order/modify":
            return self._modify_order(payload)
        if path == "/api/Position/searchOpen":
            return self._search_open_positions(payload)
        if path == "/api/Position/closeContract":
            return self._close_contract(payload)
        if path == "/api/Position/partialCloseContract":
            return self._partial_close_contract(payload)
        if path == "/api/Trade/search":
            return self._search_trades(payload)
        if path == "/api/History/retrieveBars":
            return self._retrieve_bars(payload)

        return self._error(404, f"Unsupported sim endpoint: {path}")

    def sim_update(self, account_id: int, contract_id: str, now_ts_iso: Optional[str] = None) -> None:
        with STATE_LOCK:
            bars = self._load_bars()
            if not bars:
                return
            now_ts = self._resolve_now_ts(bars, now_ts_iso)
            if now_ts is None:
                return

            key = self._bar_key(account_id, contract_id)
            last_ts = self._state["last_processed_bar_ts"].get(key, 0)

            for bar in bars:
                if bar["t"] <= last_ts:
                    continue
                if bar["t"] > now_ts:
                    break
                self._process_open_orders(account_id, contract_id, bar)
                self._process_brackets(account_id, contract_id, bar)
                last_ts = bar["t"]

            self._state["last_processed_bar_ts"][key] = last_ts
            self._save_state()

    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(self._state_path):
            with open(self._state_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        return {
            "nextOrderId": 1,
            "nextPositionId": 1,
            "nextTradeId": 1,
            "accounts": {},
            "orders": {},
            "positions": {},
            "trades": {},
            "brackets": {},
            "last_processed_bar_ts": {},
        }

    def _save_state(self) -> None:
        os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=os.path.dirname(self._state_path) or ".") as handle:
            json.dump(self._state, handle, indent=2)
            temp_path = handle.name
        os.replace(temp_path, self._state_path)

    def _ensure_accounts(self) -> None:
        accounts = self._config.get("ACCOUNTS", {})
        balance = float(self._config.get("SIM_START_BALANCE", 100000.0))
        for name, account_id in accounts.items():
            key = str(account_id)
            if key not in self._state["accounts"]:
                self._state["accounts"][key] = {
                    "id": account_id,
                    "name": name,
                    "balance": balance,
                    "canTrade": True,
                    "isVisible": True,
                    "simulated": True,
                }
        self._save_state()

    def _place_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with STATE_LOCK:
            account_id = payload.get("accountId")
            contract_id = payload.get("contractId")
            order_type = payload.get("type")
            side = payload.get("side")
            size = int(payload.get("size") or 0)
            if not account_id or not contract_id or size <= 0:
                return self._error(400, "Invalid order payload")

            order_id = self._next_id("nextOrderId")
            now_iso = self._now_iso()

            order = {
                "id": order_id,
                "orderId": order_id,
                "accountId": account_id,
                "contractId": contract_id,
                "symbolId": self._symbol_from_contract(contract_id),
                "creationTimestamp": now_iso,
                "updateTimestamp": now_iso,
                "status": ORDER_STATUS_OPEN,
                "type": order_type,
                "side": side,
                "size": size,
                "limitPrice": payload.get("limitPrice"),
                "stopPrice": payload.get("stopPrice"),
                "filledPrice": None,
                "fillPrice": None,
                "fillVolume": 0,
                "customTag": payload.get("customTag"),
            }

            self._state["orders"][str(order_id)] = order

            if order_type == ORDER_TYPE_MARKET:
                self._fill_order(order, fill_price=self._current_price())
            elif order_type in {ORDER_TYPE_LIMIT, ORDER_TYPE_STOP}:
                pass
            else:
                order["status"] = ORDER_STATUS_REJECTED
                order["updateTimestamp"] = self._now_iso()

            self._save_state()

            response = {
                "orderId": order_id,
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }
            if order.get("filledPrice") is not None:
                response["fillPrice"] = order["filledPrice"]
            return response

    def _search_orders(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = payload.get("accountId")
        start_ts = self._parse_timestamp(payload.get("startTimestamp"))
        end_ts = self._parse_timestamp(payload.get("endTimestamp"))

        orders = []
        for order in self._state["orders"].values():
            if account_id and order.get("accountId") != account_id:
                continue
            order_ts = self._parse_timestamp(order.get("creationTimestamp"))
            if start_ts and order_ts and order_ts < start_ts:
                continue
            if end_ts and order_ts and order_ts > end_ts:
                continue
            orders.append(order)

        return self._success({"orders": orders})

    def _search_open_orders(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = payload.get("accountId")
        if account_id:
            self._update_for_account(account_id)
        orders = [
            order
            for order in self._state["orders"].values()
            if order.get("status") == ORDER_STATUS_OPEN
            and (not account_id or order.get("accountId") == account_id)
        ]
        return self._success({"orders": orders})

    def _cancel_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with STATE_LOCK:
            order_id = payload.get("orderId")
            order = self._state["orders"].get(str(order_id))
            if not order:
                return self._error(404, "Order not found")
            if order["status"] == ORDER_STATUS_OPEN:
                order["status"] = ORDER_STATUS_CANCELLED
                order["updateTimestamp"] = self._now_iso()
                self._save_state()
            return self._success({"orderId": order_id})

    def _modify_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with STATE_LOCK:
            order_id = payload.get("orderId")
            order = self._state["orders"].get(str(order_id))
            if not order:
                return self._error(404, "Order not found")
            if order["status"] != ORDER_STATUS_OPEN:
                return self._error(409, "Order not open")

            for field in ("size", "limitPrice", "stopPrice"):
                if field in payload:
                    order[field] = payload[field]
            order["updateTimestamp"] = self._now_iso()
            self._save_state()
            return self._success({"orderId": order_id})

    def _search_open_positions(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = payload.get("accountId")
        if account_id:
            self._update_for_account(account_id)
        positions = [
            position
            for position in self._state["positions"].values()
            if position.get("size", 0) > 0
            and (not account_id or position.get("accountId") == account_id)
        ]
        return self._success({"positions": positions})

    def _close_contract(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with STATE_LOCK:
            account_id = payload.get("accountId")
            contract_id = payload.get("contractId")
            positions = self._open_positions(account_id, contract_id)
            if not positions:
                return self._success({"success": True})

            for position in positions:
                self._close_position(position, self._current_price())
            self._save_state()
            return self._success({"success": True})

    def _partial_close_contract(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with STATE_LOCK:
            account_id = payload.get("accountId")
            contract_id = payload.get("contractId")
            size = int(payload.get("size") or 0)
            if size <= 0:
                return self._error(400, "Invalid size")

            positions = self._open_positions(account_id, contract_id)
            if not positions:
                return self._success({"success": True})

            for position in positions:
                self._close_position(position, self._current_price(), close_size=size)
            self._save_state()
            return self._success({"success": True})

    def _search_trades(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = payload.get("accountId")
        if account_id:
            self._update_for_account(account_id)
        start_ts = self._parse_timestamp(payload.get("startTimestamp"))
        end_ts = self._parse_timestamp(payload.get("endTimestamp"))

        trades = []
        for trade in self._state["trades"].values():
            if account_id and trade.get("accountId") != account_id:
                continue
            trade_ts = self._parse_timestamp(trade.get("creationTimestamp"))
            if start_ts and trade_ts and trade_ts < start_ts:
                continue
            if end_ts and trade_ts and trade_ts > end_ts:
                continue
            trades.append(trade)

        return self._success({"trades": trades})

    def _retrieve_bars(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        bars = self._load_bars()
        if not bars:
            return self._success({"bars": []})

        start_ts = self._parse_timestamp(payload.get("startTime"))
        end_ts = self._parse_timestamp(payload.get("endTime"))
        limit = payload.get("limit")
        if isinstance(limit, str):
            limit = int(limit) if limit.isdigit() else None

        filtered = []
        for bar in bars:
            bar_dt = datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc)
            if start_ts and bar_dt < start_ts:
                continue
            if end_ts and bar_dt > end_ts:
                continue
            filtered.append(bar)

        if limit:
            filtered = filtered[-limit:]

        return self._success({"bars": filtered})

    def _process_open_orders(self, account_id: int, contract_id: str, bar: Dict[str, Any]) -> None:
        for order in self._state["orders"].values():
            if order.get("status") != ORDER_STATUS_OPEN:
                continue
            if order.get("accountId") != account_id or order.get("contractId") != contract_id:
                continue
            if order.get("customTag") in {"SIM_BRACKET_SL", "SIM_BRACKET_TP"}:
                continue

            if order.get("type") == ORDER_TYPE_LIMIT:
                if self._limit_hit(order, bar):
                    self._fill_order(order, fill_price=order.get("limitPrice"))
            elif order.get("type") == ORDER_TYPE_STOP:
                if self._stop_hit(order, bar):
                    self._fill_order(order, fill_price=order.get("stopPrice"))

    def _process_brackets(self, account_id: int, contract_id: str, bar: Dict[str, Any]) -> None:
        fill_policy = self._config.get("SIM_FILL_POLICY", "worst")

        for position in self._open_positions(account_id, contract_id):
            bracket = self._state["brackets"].get(str(position["id"]))
            if not bracket:
                self._create_brackets(position)
                continue

            sl_order = self._state["orders"].get(str(bracket["sl_order_id"]))
            tp_order = self._state["orders"].get(str(bracket["tp_order_id"]))
            if not sl_order or not tp_order:
                continue

            sl_hit = self._stop_hit(sl_order, bar)
            tp_hit = self._limit_hit(tp_order, bar)
            if not sl_hit and not tp_hit:
                continue

            if sl_hit and tp_hit:
                use_sl = fill_policy == "worst"
                trigger_order = sl_order if use_sl else tp_order
            else:
                trigger_order = sl_order if sl_hit else tp_order

            trigger_price = (
                trigger_order.get("stopPrice")
                if trigger_order.get("type") == ORDER_TYPE_STOP
                else trigger_order.get("limitPrice")
            )
            self._close_position(position, trigger_price, exit_order=trigger_order)

    def _fill_order(self, order: Dict[str, Any], fill_price: Optional[float]) -> None:
        fill_price = float(fill_price) if fill_price is not None else None
        order["filledPrice"] = fill_price
        order["fillPrice"] = fill_price
        order["fillVolume"] = order.get("size")
        order["status"] = ORDER_STATUS_FILLED
        order["updateTimestamp"] = self._now_iso()

        self._record_trade(order, fill_price, profit_and_loss=None)
        self._update_position_from_fill(order, fill_price)
        self._save_state()

    def _update_position_from_fill(self, order: Dict[str, Any], fill_price: Optional[float]) -> None:
        if fill_price is None:
            return

        account_id = order.get("accountId")
        contract_id = order.get("contractId")
        side = order.get("side")
        size = order.get("size") or 0

        position_type = POSITION_TYPE_LONG if side == ORDER_SIDE_BID else POSITION_TYPE_SHORT
        open_positions = self._open_positions(account_id, contract_id)

        if open_positions:
            position = open_positions[0]
            total_size = position["size"] + size
            avg_price = (
                (position["averagePrice"] * position["size"]) + (fill_price * size)
            ) / total_size
            position["size"] = total_size
            position["averagePrice"] = avg_price
            position["avgPrice"] = avg_price
            position["updateTimestamp"] = self._now_iso()
        else:
            position_id = self._next_id("nextPositionId")
            now_iso = self._now_iso()
            position = {
                "id": position_id,
                "accountId": account_id,
                "contractId": contract_id,
                "creationTimestamp": now_iso,
                "updateTimestamp": now_iso,
                "type": position_type,
                "size": size,
                "averagePrice": fill_price,
                "avgPrice": fill_price,
            }
            self._state["positions"][str(position_id)] = position

        self._state["last_processed_bar_ts"][self._bar_key(account_id, contract_id)] = (
            self._current_bar_timestamp()
        )
        self._create_brackets(position)

    def _create_brackets(self, position: Dict[str, Any]) -> None:
        position_id = position["id"]
        if str(position_id) in self._state["brackets"]:
            return

        sl_offset, tp_offset = self._bracket_offsets(position["contractId"])
        entry_price = position.get("averagePrice") or 0
        size = position.get("size")
        side = position.get("type")
        now_iso = self._now_iso()

        if side == POSITION_TYPE_LONG:
            sl_price = entry_price - sl_offset
            tp_price = entry_price + tp_offset
            close_side = ORDER_SIDE_ASK
        else:
            sl_price = entry_price + sl_offset
            tp_price = entry_price - tp_offset
            close_side = ORDER_SIDE_BID

        sl_price = self._round_to_tick(sl_price, position["contractId"])
        tp_price = self._round_to_tick(tp_price, position["contractId"])

        sl_order_id = self._next_id("nextOrderId")
        tp_order_id = self._next_id("nextOrderId")

        sl_order = {
            "id": sl_order_id,
            "orderId": sl_order_id,
            "accountId": position["accountId"],
            "contractId": position["contractId"],
            "symbolId": self._symbol_from_contract(position["contractId"]),
            "creationTimestamp": now_iso,
            "updateTimestamp": now_iso,
            "status": ORDER_STATUS_OPEN,
            "type": ORDER_TYPE_STOP,
            "side": close_side,
            "size": size,
            "limitPrice": None,
            "stopPrice": sl_price,
            "filledPrice": None,
            "fillPrice": None,
            "fillVolume": 0,
            "customTag": "SIM_BRACKET_SL",
        }

        tp_order = {
            "id": tp_order_id,
            "orderId": tp_order_id,
            "accountId": position["accountId"],
            "contractId": position["contractId"],
            "symbolId": self._symbol_from_contract(position["contractId"]),
            "creationTimestamp": now_iso,
            "updateTimestamp": now_iso,
            "status": ORDER_STATUS_OPEN,
            "type": ORDER_TYPE_LIMIT,
            "side": close_side,
            "size": size,
            "limitPrice": tp_price,
            "stopPrice": None,
            "filledPrice": None,
            "fillPrice": None,
            "fillVolume": 0,
            "customTag": "SIM_BRACKET_TP",
        }

        self._state["orders"][str(sl_order_id)] = sl_order
        self._state["orders"][str(tp_order_id)] = tp_order
        self._state["brackets"][str(position_id)] = {
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
        }
        self._save_state()

    def _close_position(
        self,
        position: Dict[str, Any],
        exit_price: Optional[float],
        close_size: Optional[int] = None,
        exit_order: Optional[Dict[str, Any]] = None,
    ) -> None:
        exit_price = float(exit_price) if exit_price is not None else None
        if exit_price is None:
            return

        size_to_close = close_size or position.get("size")
        if size_to_close <= 0:
            return

        entry_price = position.get("averagePrice") or 0
        multiplier = self._contract_multiplier(position["contractId"])
        if position.get("type") == POSITION_TYPE_LONG:
            pnl = (exit_price - entry_price) * size_to_close * multiplier
            exit_side = ORDER_SIDE_ASK
        else:
            pnl = (entry_price - exit_price) * size_to_close * multiplier
            exit_side = ORDER_SIDE_BID

        if exit_order is None:
            exit_order_id = self._next_id("nextOrderId")
            now_iso = self._now_iso()
            exit_order = {
                "id": exit_order_id,
                "orderId": exit_order_id,
                "accountId": position["accountId"],
                "contractId": position["contractId"],
                "symbolId": self._symbol_from_contract(position["contractId"]),
                "creationTimestamp": now_iso,
                "updateTimestamp": now_iso,
                "status": ORDER_STATUS_FILLED,
                "type": ORDER_TYPE_MARKET,
                "side": exit_side,
                "size": size_to_close,
                "limitPrice": None,
                "stopPrice": None,
                "filledPrice": exit_price,
                "fillPrice": exit_price,
                "fillVolume": size_to_close,
                "customTag": "SIM_CLOSE",
            }
            self._state["orders"][str(exit_order_id)] = exit_order
        else:
            exit_order["status"] = ORDER_STATUS_FILLED
            exit_order["filledPrice"] = exit_price
            exit_order["fillPrice"] = exit_price
            exit_order["fillVolume"] = size_to_close
            exit_order["updateTimestamp"] = self._now_iso()

        position["size"] -= size_to_close
        position["updateTimestamp"] = self._now_iso()
        if position["size"] <= 0:
            position["size"] = 0
            position["closedTimestamp"] = self._now_iso()
        else:
            self._resize_brackets(position["id"], position["size"])

        self._record_trade(exit_order, exit_price, profit_and_loss=pnl)
        self._update_account_balance(position["accountId"], pnl)
        if position["size"] <= 0:
            self._cancel_brackets(position["id"], exclude_order_id=exit_order.get("id"))

    def _cancel_brackets(self, position_id: int, exclude_order_id: Optional[int] = None) -> None:
        bracket = self._state["brackets"].get(str(position_id))
        if not bracket:
            return

        for key in ("sl_order_id", "tp_order_id"):
            order_id = bracket.get(key)
            if order_id == exclude_order_id:
                continue
            order = self._state["orders"].get(str(order_id))
            if order and order.get("status") == ORDER_STATUS_OPEN:
                order["status"] = ORDER_STATUS_CANCELLED
                order["updateTimestamp"] = self._now_iso()

    def _resize_brackets(self, position_id: int, new_size: int) -> None:
        bracket = self._state["brackets"].get(str(position_id))
        if not bracket:
            return
        for key in ("sl_order_id", "tp_order_id"):
            order_id = bracket.get(key)
            order = self._state["orders"].get(str(order_id))
            if order and order.get("status") == ORDER_STATUS_OPEN:
                order["size"] = new_size
                order["updateTimestamp"] = self._now_iso()

    def _record_trade(
        self,
        order: Dict[str, Any],
        price: Optional[float],
        profit_and_loss: Optional[float],
    ) -> None:
        trade_id = self._next_id("nextTradeId")
        now_iso = self._now_iso()
        trade = {
            "id": trade_id,
            "accountId": order.get("accountId"),
            "contractId": order.get("contractId"),
            "creationTimestamp": now_iso,
            "price": price,
            "profitAndLoss": profit_and_loss,
            "fees": 0.0,
            "side": order.get("side"),
            "size": order.get("size"),
            "voided": False,
            "orderId": order.get("id"),
        }
        self._state["trades"][str(trade_id)] = trade

    def _update_account_balance(self, account_id: int, pnl: float) -> None:
        account = self._state["accounts"].get(str(account_id))
        if not account:
            return
        account["balance"] = float(account.get("balance", 0)) + float(pnl)

    def _open_positions(self, account_id: int, contract_id: str) -> List[Dict[str, Any]]:
        return [
            position
            for position in self._state["positions"].values()
            if position.get("accountId") == account_id
            and position.get("contractId") == contract_id
            and position.get("size", 0) > 0
        ]

    def _update_for_account(self, account_id: int) -> None:
        contracts = {
            position.get("contractId")
            for position in self._state["positions"].values()
            if position.get("accountId") == account_id and position.get("size", 0) > 0
        }
        if not contracts:
            contracts.add(self._config.get("OVERRIDE_CONTRACT_ID"))
        for contract_id in contracts:
            if contract_id:
                self.sim_update(account_id, contract_id)

    def _build_contract(self, contract_id: Optional[str]) -> Dict[str, Any]:
        contract_id = contract_id or "CON.F.US.MES.H26"
        symbol = self._symbol_from_contract(contract_id)
        tick_size = self._contract_tick_size(contract_id)
        tick_value = self._contract_tick_value(contract_id)
        return {
            "id": contract_id,
            "symbol": symbol,
            "symbolId": symbol,
            "tickSize": tick_size,
            "tickValue": tick_value,
            "description": f"Simulated {symbol} contract",
        }

    def _bracket_offsets(self, contract_id: str) -> tuple[float, float]:
        tick_size = self._contract_tick_size(contract_id)
        tick_value = self._contract_tick_value(contract_id)
        sl_usd = float(self._config.get("SIM_BRACKET_SL_USD", 30))
        tp_usd = float(self._config.get("SIM_BRACKET_TP_USD", 60))

        sl_ticks = max(1, int(round(sl_usd / tick_value)))
        tp_ticks = max(1, int(round(tp_usd / tick_value)))
        return sl_ticks * tick_size, tp_ticks * tick_size

    def _contract_tick_size(self, contract_id: str) -> float:
        if "MES" in contract_id:
            return float(self._config.get("SIM_TICK_SIZE", 0.25))
        return float(self._config.get("SIM_TICK_SIZE", 0.25))

    def _contract_tick_value(self, contract_id: str) -> float:
        if "MES" in contract_id:
            return float(self._config.get("SIM_TICK_VALUE", 1.25))
        return float(self._config.get("SIM_TICK_VALUE", 1.25))

    def _contract_multiplier(self, contract_id: str) -> float:
        tick_size = self._contract_tick_size(contract_id)
        tick_value = self._contract_tick_value(contract_id)
        if tick_size == 0:
            return 1.0
        return tick_value / tick_size

    def _round_to_tick(self, price: float, contract_id: str) -> float:
        tick_size = self._contract_tick_size(contract_id)
        if tick_size == 0:
            return price
        return round(price / tick_size) * tick_size

    def _limit_hit(self, order: Dict[str, Any], bar: Dict[str, Any]) -> bool:
        limit_price = order.get("limitPrice")
        if limit_price is None:
            return False
        if order.get("side") == ORDER_SIDE_BID:
            return bar["l"] <= limit_price
        return bar["h"] >= limit_price

    def _stop_hit(self, order: Dict[str, Any], bar: Dict[str, Any]) -> bool:
        stop_price = order.get("stopPrice")
        if stop_price is None:
            return False
        if order.get("side") == ORDER_SIDE_BID:
            return bar["h"] >= stop_price
        return bar["l"] <= stop_price

    def _current_price(self) -> float:
        bars = self._load_bars()
        if not bars:
            return 0.0
        return float(bars[-1]["c"])

    def _current_bar_timestamp(self) -> int:
        bars = self._load_bars()
        if not bars:
            return 0
        return bars[-1]["t"]

    def _load_bars(self) -> List[Dict[str, Any]]:
        paths_env = os.getenv("SIM_DATAFEED_PATHS")
        default_paths = [
            "/mnt/data/tv_datafeed_5m_rows (2).csv",
            "/mnt/data/tv_datafeed_15m_rows.csv",
            "/mnt/data/tv_datafeed_30m_rows.csv",
            "/mnt/data/tv_datafeed_1D_rows.csv",
        ]
        paths = [p.strip() for p in paths_env.split(",")] if paths_env else default_paths
        for path in paths:
            if not path or not os.path.exists(path):
                continue
            mtime = os.path.getmtime(path)
            cache = self._bars_cache.get(path)
            if cache and cache.get("mtime") == mtime:
                return cache["bars"]
            bars = self._read_bars_from_csv(path)
            self._bars_cache[path] = {"mtime": mtime, "bars": bars}
            return bars
        return []

    def _read_bars_from_csv(self, path: str) -> List[Dict[str, Any]]:
        bars = []
        with open(path, "r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                try:
                    ts = self._parse_timestamp(row.get("t") or row.get("ts") or row.get("time"))
                    if not ts:
                        continue
                    ts_ms = int(ts.timestamp() * 1000)
                    bars.append({
                        "t": ts_ms,
                        "o": float(row.get("o") or row.get("open") or 0),
                        "h": float(row.get("h") or row.get("high") or 0),
                        "l": float(row.get("l") or row.get("low") or 0),
                        "c": float(row.get("c") or row.get("close") or 0),
                        "v": float(row.get("v") or row.get("volume") or 0),
                    })
                except Exception:
                    continue
        bars.sort(key=lambda b: b["t"])
        return bars

    def _parse_timestamp(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc)
        if isinstance(value, (int, float)):
            if value > 1e12:
                return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            return datetime.fromtimestamp(value, tz=timezone.utc)
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(raw).astimezone(timezone.utc)
            except ValueError:
                pass
            if raw.isdigit():
                return self._parse_timestamp(int(raw))
        return None

    def _resolve_now_ts(self, bars: List[Dict[str, Any]], now_ts_iso: Optional[str]) -> Optional[int]:
        if now_ts_iso:
            ts = self._parse_timestamp(now_ts_iso)
            if ts:
                return int(ts.timestamp() * 1000)
        return bars[-1]["t"] if bars else None

    def _bar_key(self, account_id: int, contract_id: str) -> str:
        return f"{account_id}:{contract_id}"

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _next_id(self, key: str) -> int:
        current = int(self._state.get(key, 1))
        self._state[key] = current + 1
        return current

    def _symbol_from_contract(self, contract_id: str) -> str:
        match = re.search(r"\.([A-Z]+)\.", contract_id)
        if match:
            return match.group(1)
        return contract_id.split(".")[-1]

    def _success(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        base = {"success": True, "errorCode": 0, "errorMessage": None}
        if payload:
            base.update(payload)
        return base

    def _error(self, code: int, message: str) -> Dict[str, Any]:
        return {"success": False, "errorCode": code, "errorMessage": message}
