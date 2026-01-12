import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from dateutil import parser
from supabase import create_client


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return parser.isoparse(value)


def _round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return round(price / tick_size) * tick_size


def _safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


@dataclass
class ContractInfo:
    contract_id: str
    name: str
    description: str
    tick_size: float
    tick_value: float
    symbol_id: str
    active_contract: bool = True


class SupabaseBarLoader:
    def __init__(self, supabase_url: Optional[str], supabase_key: Optional[str]):
        self._supabase = None
        if supabase_url and supabase_key:
            try:
                self._supabase = create_client(supabase_url, supabase_key)
            except Exception as exc:
                logging.error("Supabase client init failed for SimBroker: %s", exc)

    def get_latest_bar(self, symbol: str, timeframes: Optional[List[str]] = None) -> Optional[Dict]:
        if not self._supabase:
            return None
        timeframes = timeframes or ["1m", "1"]
        timeframe_or_clause = ",".join(f"timeframe.eq.\"{tf}\"" for tf in timeframes)
        try:
            result = (
                self._supabase
                .table("tv_datafeed")
                .select("ts,o,h,l,c,v")
                .eq("symbol", symbol)
                .or_(timeframe_or_clause)
                .order("ts", desc=True)
                .limit(1)
                .execute()
            )
            if not result.data:
                return None
            row = result.data[0]
            return {
                "t": row.get("ts"),
                "o": row.get("o"),
                "h": row.get("h"),
                "l": row.get("l"),
                "c": row.get("c"),
                "v": row.get("v") or 0,
            }
        except Exception as exc:
            logging.error("Supabase latest bar query failed: %s", exc)
            return None

    def get_bars(
        self,
        symbol: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        timeframe: str = "1m",
        limit: Optional[int] = None,
    ) -> List[Dict]:
        if not self._supabase:
            return []
        timeframe_variants = {timeframe}
        if timeframe.endswith("m"):
            timeframe_variants.add(timeframe[:-1])
        else:
            timeframe_variants.add(f"{timeframe}m")
        timeframe_or_clause = ",".join(f"timeframe.eq.\"{tf}\"" for tf in timeframe_variants)
        query = (
            self._supabase
            .table("tv_datafeed")
            .select("ts,o,h,l,c,v")
            .eq("symbol", symbol)
            .or_(timeframe_or_clause)
            .order("ts", desc=False)
        )
        if start_time:
            query = query.gte("ts", start_time.isoformat())
        if end_time:
            query = query.lte("ts", end_time.isoformat())
        if limit:
            query = query.limit(limit)
        try:
            result = query.execute()
        except Exception as exc:
            logging.error("Supabase bar query failed: %s", exc)
            return []
        bars = []
        for row in result.data or []:
            bars.append(
                {
                    "t": row.get("ts"),
                    "o": row.get("o"),
                    "h": row.get("h"),
                    "l": row.get("l"),
                    "c": row.get("c"),
                    "v": row.get("v") or 0,
                }
            )
        return bars


class InMemoryBarLoader:
    def __init__(self, bars: Optional[List[Dict]] = None):
        self._bars = bars or []

    def get_latest_bar(self, symbol: str, timeframes: Optional[List[str]] = None) -> Optional[Dict]:
        if not self._bars:
            return None
        return self._bars[-1]

    def get_bars(
        self,
        symbol: str,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        timeframe: str = "1m",
        limit: Optional[int] = None,
    ) -> List[Dict]:
        filtered = []
        for bar in self._bars:
            ts = _parse_ts(bar.get("t"))
            if start_time and ts and ts < start_time:
                continue
            if end_time and ts and ts > end_time:
                continue
            filtered.append(bar)
        if limit:
            return filtered[:limit]
        return filtered


class SimBroker:
    _LOCK = threading.Lock()

    def __init__(self, config: Dict, bar_loader=None, state_path: Optional[str] = None):
        self._config = config
        self._state_path = state_path or config.get("SIMBROKER_STATE_PATH", "simbroker_state.json")
        self._bar_loader = bar_loader or SupabaseBarLoader(
            config.get("SUPABASE_URL"), config.get("SUPABASE_KEY")
        )
        self._state = self._load_state()
        self._ensure_accounts()

    def dispatch(self, path: str, payload: Dict) -> Dict:
        logging.debug("SimBroker dispatch %s payload=%s", path, payload)

        if path == "/api/Auth/loginKey":
            return {
                "token": "SIM_TOKEN",
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }
        if path == "/api/Auth/validate":
            return {
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
                "newToken": "SIM_TOKEN",
            }
        if path == "/api/Account/search":
            return self._response({"accounts": self._state["accounts"]})
        if path == "/api/Contract/available":
            return self._response({"contracts": self._available_contracts()})
        if path == "/api/Contract/search":
            return self._response({"contracts": self._search_contracts(payload)})
        if path == "/api/Contract/searchById":
            return self._response({"contract": self._contract_by_id(payload.get("contractId"))})
        if path == "/api/Order/place":
            return self._order_place(payload)
        if path == "/api/Order/search":
            return self._order_search(payload)
        if path == "/api/Order/searchOpen":
            self._refresh_account(payload.get("accountId"))
            return self._order_search_open(payload)
        if path == "/api/Order/cancel":
            return self._order_cancel(payload)
        if path == "/api/Order/modify":
            return self._order_modify(payload)
        if path == "/api/Position/searchOpen":
            self._refresh_account(payload.get("accountId"))
            return self._position_search_open(payload)
        if path == "/api/Position/closeContract":
            return self._position_close(payload)
        if path == "/api/Position/partialCloseContract":
            return self._position_partial_close(payload)
        if path == "/api/Trade/search":
            self._refresh_account(payload.get("accountId"))
            return self._trade_search(payload)
        if path == "/api/History/retrieveBars":
            return self._history_retrieve(payload)

        return {
            "success": False,
            "errorCode": 404,
            "errorMessage": f"SimBroker path not implemented: {path}",
        }

    def sim_update(self, account_id: int, contract_id: str, now_ts_iso: Optional[str] = None) -> None:
        if account_id is None or not contract_id:
            return
        now_ts = _parse_ts(now_ts_iso) if now_ts_iso else datetime.now(timezone.utc)
        symbol = self._symbol_from_contract(contract_id)
        last_key = f"{account_id}|{contract_id}"
        last_ts_raw = self._state["last_processed_bar_ts"].get(last_key)
        last_ts = _parse_ts(last_ts_raw) if last_ts_raw else None
        bars = self._bar_loader.get_bars(symbol, last_ts, now_ts, timeframe="1m")
        if not bars:
            return

        for bar in bars:
            self._process_bar(account_id, contract_id, bar)
            self._state["last_processed_bar_ts"][last_key] = bar["t"]

        self._save_state()

    def _load_state(self) -> Dict:
        if not os.path.exists(self._state_path):
            return {
                "nextOrderId": 1,
                "nextPositionId": 1,
                "nextTradeId": 1,
                "accounts": [],
                "orders": [],
                "positions": [],
                "trades": [],
                "brackets": [],
                "last_processed_bar_ts": {},
            }
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            logging.error("Failed to load SimBroker state: %s", exc)
            return {
                "nextOrderId": 1,
                "nextPositionId": 1,
                "nextTradeId": 1,
                "accounts": [],
                "orders": [],
                "positions": [],
                "trades": [],
                "brackets": [],
                "last_processed_bar_ts": {},
            }

    def _save_state(self) -> None:
        with self._LOCK:
            tmp_path = f"{self._state_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, indent=2, sort_keys=True)
            os.replace(tmp_path, self._state_path)

    def _ensure_accounts(self) -> None:
        account_map = self._config.get("ACCOUNTS", {})
        existing_ids = {acct["id"] for acct in self._state["accounts"]}
        sim_balance = float(self._config.get("SIM_ACCOUNT_BALANCE", 50000.0))
        for name, acct_id in account_map.items():
            if acct_id in existing_ids:
                continue
            self._state["accounts"].append(
                {
                    "id": acct_id,
                    "name": name,
                    "balance": sim_balance,
                    "canTrade": True,
                    "isVisible": True,
                    "simulated": True,
                }
            )
        self._save_state()

    def _response(self, payload: Dict) -> Dict:
        return {"success": True, "errorCode": 0, "errorMessage": None, **payload}

    def _symbol_from_contract(self, contract_id: str) -> str:
        if not contract_id:
            return "MES"
        for symbol in ("MES", "MNQ", "NQ", "ES", "RTY", "YM"):
            if symbol in contract_id:
                return symbol
        return "MES"

    def _contract_info(self, contract_id: str) -> ContractInfo:
        symbol = self._symbol_from_contract(contract_id)
        tick_size = float(self._config.get("SIM_TICK_SIZE", 0.25))
        tick_value = float(self._config.get("SIM_TICK_VALUE", 1.25))
        if symbol != "MES":
            tick_size = tick_size or 0.25
            tick_value = tick_value or 1.25
        name = contract_id.split(".")[-1] if contract_id else symbol
        description = f"Simulated {symbol} contract"
        return ContractInfo(
            contract_id=contract_id,
            name=name,
            description=description,
            tick_size=tick_size,
            tick_value=tick_value,
            symbol_id=f"F.US.{symbol}",
            active_contract=True,
        )

    def _available_contracts(self) -> List[Dict]:
        contract_id = self._config.get("OVERRIDE_CONTRACT_ID") or "CON.F.US.MES.H26"
        contract = self._contract_info(contract_id)
        return [self._contract_to_payload(contract)]

    def _search_contracts(self, payload: Dict) -> List[Dict]:
        search_text = (payload.get("searchText") or "").upper()
        contracts = self._available_contracts()
        if not search_text:
            return contracts
        return [c for c in contracts if search_text in (c.get("name") or "") or search_text in (c.get("id") or "")]

    def _contract_by_id(self, contract_id: Optional[str]) -> Optional[Dict]:
        if not contract_id:
            return None
        return self._contract_to_payload(self._contract_info(contract_id))

    def _contract_to_payload(self, contract: ContractInfo) -> Dict:
        return {
            "id": contract.contract_id,
            "name": contract.name,
            "description": contract.description,
            "tickSize": contract.tick_size,
            "tickValue": contract.tick_value,
            "activeContract": contract.active_contract,
            "symbolId": contract.symbol_id,
        }

    def _order_place(self, payload: Dict) -> Dict:
        account_id = payload.get("accountId")
        contract_id = payload.get("contractId")
        order_type = payload.get("type")
        side = payload.get("side")
        size = int(payload.get("size") or 0)
        limit_price = payload.get("limitPrice")
        stop_price = payload.get("stopPrice")
        custom_tag = payload.get("customTag")

        if account_id is None or not contract_id or size <= 0:
            return {
                "success": False,
                "errorCode": 400,
                "errorMessage": "Invalid order payload",
            }

        order_id = self._next_id("nextOrderId")
        now_ts = _utc_now_iso()
        contract = self._contract_info(contract_id)
        order = {
            "id": order_id,
            "accountId": account_id,
            "contractId": contract_id,
            "symbolId": contract.symbol_id,
            "creationTimestamp": now_ts,
            "updateTimestamp": now_ts,
            "status": ORDER_STATUS_OPEN,
            "type": order_type,
            "side": side,
            "size": size,
            "limitPrice": limit_price,
            "stopPrice": stop_price,
            "fillVolume": 0,
            "filledPrice": None,
            "customTag": custom_tag,
        }

        if order_type == ORDER_TYPE_MARKET:
            fill_price = self._market_fill_price(contract_id)
            order["status"] = ORDER_STATUS_FILLED
            order["fillVolume"] = size
            order["filledPrice"] = fill_price
            order["updateTimestamp"] = now_ts
            self._state["orders"].append(order)
            self._apply_fill(account_id, contract_id, side, size, fill_price, order_id)
            self._create_bracket_orders(account_id, contract_id, side, size, fill_price, order_id)
            self._save_state()
        else:
            self._state["orders"].append(order)
            self._save_state()

        return {
            "orderId": order_id,
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
        }

    def _order_search(self, payload: Dict) -> Dict:
        account_id = payload.get("accountId")
        start_ts = _parse_ts(payload.get("startTimestamp"))
        end_ts = _parse_ts(payload.get("endTimestamp"))
        orders = []
        for order in self._state["orders"]:
            if account_id is not None and order.get("accountId") != account_id:
                continue
            created = _parse_ts(order.get("creationTimestamp"))
            if start_ts and created and created < start_ts:
                continue
            if end_ts and created and created > end_ts:
                continue
            orders.append(self._public_order(order))
        return self._response({"orders": orders})

    def _order_search_open(self, payload: Dict) -> Dict:
        account_id = payload.get("accountId")
        orders = [
            self._public_order(order)
            for order in self._state["orders"]
            if order.get("status") == ORDER_STATUS_OPEN
            and (account_id is None or order.get("accountId") == account_id)
        ]
        return self._response({"orders": orders})

    def _order_cancel(self, payload: Dict) -> Dict:
        account_id = payload.get("accountId")
        order_id = payload.get("orderId")
        for order in self._state["orders"]:
            if order.get("id") == order_id and order.get("accountId") == account_id:
                if order.get("status") == ORDER_STATUS_OPEN:
                    order["status"] = ORDER_STATUS_CANCELLED
                    order["updateTimestamp"] = _utc_now_iso()
                    self._save_state()
                return self._response({})
        return {
            "success": False,
            "errorCode": 404,
            "errorMessage": "Order not found",
        }

    def _order_modify(self, payload: Dict) -> Dict:
        account_id = payload.get("accountId")
        order_id = payload.get("orderId")
        for order in self._state["orders"]:
            if order.get("id") == order_id and order.get("accountId") == account_id:
                if order.get("status") != ORDER_STATUS_OPEN:
                    return {
                        "success": False,
                        "errorCode": 409,
                        "errorMessage": "Order not open",
                    }
                for field in ("size", "limitPrice", "stopPrice"):
                    if field in payload and payload[field] is not None:
                        order[field] = payload[field]
                order["updateTimestamp"] = _utc_now_iso()
                self._save_state()
                return self._response({})
        return {
            "success": False,
            "errorCode": 404,
            "errorMessage": "Order not found",
        }

    def _position_search_open(self, payload: Dict) -> Dict:
        account_id = payload.get("accountId")
        positions = [
            position
            for position in self._state["positions"]
            if position.get("size", 0) > 0
            and (account_id is None or position.get("accountId") == account_id)
        ]
        return self._response({"positions": positions})

    def _position_close(self, payload: Dict) -> Dict:
        account_id = payload.get("accountId")
        contract_id = payload.get("contractId")
        position = self._find_position(account_id, contract_id)
        if not position:
            return self._response({})
        fill_price = self._market_fill_price(contract_id)
        self._close_position(position, fill_price)
        self._save_state()
        return self._response({})

    def _position_partial_close(self, payload: Dict) -> Dict:
        account_id = payload.get("accountId")
        contract_id = payload.get("contractId")
        size = int(payload.get("size") or 0)
        position = self._find_position(account_id, contract_id)
        if not position or size <= 0:
            return self._response({})

        fill_price = self._market_fill_price(contract_id)
        close_size = min(size, position["size"])
        self._close_position(position, fill_price, size_override=close_size)
        if position["size"] <= 0:
            self._remove_position(position)
        else:
            self._resize_brackets(account_id, contract_id, position["size"])
        self._save_state()
        return self._response({})

    def _trade_search(self, payload: Dict) -> Dict:
        account_id = payload.get("accountId")
        start_ts = _parse_ts(payload.get("startTimestamp"))
        end_ts = _parse_ts(payload.get("endTimestamp"))
        trades = []
        for trade in self._state["trades"]:
            if account_id is not None and trade.get("accountId") != account_id:
                continue
            created = _parse_ts(trade.get("creationTimestamp"))
            if start_ts and created and created < start_ts:
                continue
            if end_ts and created and created > end_ts:
                continue
            trades.append(trade)
        return self._response({"trades": trades})

    def _history_retrieve(self, payload: Dict) -> Dict:
        contract_id = payload.get("contractId")
        unit = payload.get("unit")
        unit_number = payload.get("unitNumber", 1)
        limit = payload.get("limit")
        start_time = _parse_ts(payload.get("startTime"))
        end_time = _parse_ts(payload.get("endTime"))
        if unit != 2:
            return self._response({"bars": []})
        timeframe = f"{unit_number}m"
        symbol = self._symbol_from_contract(contract_id)
        bars = self._bar_loader.get_bars(symbol, start_time, end_time, timeframe=timeframe, limit=limit)
        return self._response({"bars": bars})

    def _refresh_account(self, account_id: Optional[int]) -> None:
        if account_id is None:
            return
        contracts = {
            position.get("contractId")
            for position in self._state["positions"]
            if position.get("accountId") == account_id
        }
        brackets = {
            order.get("contractId")
            for order in self._state["orders"]
            if order.get("accountId") == account_id and order.get("status") == ORDER_STATUS_OPEN
        }
        for contract_id in contracts.union(brackets):
            if contract_id:
                self.sim_update(account_id, contract_id)

    def _process_bar(self, account_id: int, contract_id: str, bar: Dict) -> None:
        position = self._find_position(account_id, contract_id)
        if not position:
            return
        bracket_orders = [
            order
            for order in self._state["orders"]
            if order.get("accountId") == account_id
            and order.get("contractId") == contract_id
            and order.get("status") == ORDER_STATUS_OPEN
            and order.get("synthetic")
        ]
        if not bracket_orders:
            return

        high = _safe_float(bar.get("h"))
        low = _safe_float(bar.get("l"))
        stop_order = next((o for o in bracket_orders if o.get("type") == ORDER_TYPE_STOP), None)
        limit_order = next((o for o in bracket_orders if o.get("type") == ORDER_TYPE_LIMIT), None)
        if not stop_order and not limit_order:
            return

        fill_policy = self._config.get("SIM_FILL_POLICY", "worst").lower()
        trigger = None
        if position["type"] == POSITION_TYPE_LONG:
            hit_tp = limit_order and high >= _safe_float(limit_order.get("limitPrice"))
            hit_sl = stop_order and low <= _safe_float(stop_order.get("stopPrice"))
        else:
            hit_tp = limit_order and low <= _safe_float(limit_order.get("limitPrice"))
            hit_sl = stop_order and high >= _safe_float(stop_order.get("stopPrice"))

        if hit_tp and hit_sl:
            trigger = stop_order if fill_policy == "worst" else limit_order
        elif hit_tp:
            trigger = limit_order
        elif hit_sl:
            trigger = stop_order

        if not trigger:
            return

        exit_price = (
            trigger.get("limitPrice")
            if trigger.get("type") == ORDER_TYPE_LIMIT
            else trigger.get("stopPrice")
        )
        exit_price = _safe_float(exit_price)
        exit_size = position.get("size", 0)
        self._close_position(position, exit_price, order_override=trigger)
        for order in bracket_orders:
            if order.get("id") == trigger.get("id"):
                order["status"] = ORDER_STATUS_FILLED
                order["filledPrice"] = exit_price
                order["fillVolume"] = exit_size
                order["updateTimestamp"] = _utc_now_iso()
            else:
                order["status"] = ORDER_STATUS_CANCELLED
                order["updateTimestamp"] = _utc_now_iso()

    def _create_bracket_orders(
        self,
        account_id: int,
        contract_id: str,
        side: int,
        size: int,
        entry_price: float,
        parent_order_id: int,
    ) -> None:
        if size <= 0:
            return
        position_type = POSITION_TYPE_LONG if side == ORDER_SIDE_BID else POSITION_TYPE_SHORT
        contract = self._contract_info(contract_id)
        stop_offset = self._bracket_offset(contract.tick_size, contract.tick_value, "SIM_BRACKET_SL_USD")
        limit_offset = self._bracket_offset(contract.tick_size, contract.tick_value, "SIM_BRACKET_TP_USD")
        if position_type == POSITION_TYPE_LONG:
            stop_price = entry_price - stop_offset
            limit_price = entry_price + limit_offset
            bracket_side = ORDER_SIDE_ASK
        else:
            stop_price = entry_price + stop_offset
            limit_price = entry_price - limit_offset
            bracket_side = ORDER_SIDE_BID

        stop_price = _round_to_tick(stop_price, contract.tick_size)
        limit_price = _round_to_tick(limit_price, contract.tick_size)
        now_ts = _utc_now_iso()

        stop_order = self._build_order(
            account_id,
            contract,
            ORDER_TYPE_STOP,
            bracket_side,
            size,
            stop_price=stop_price,
            custom_tag="sim_bracket_sl",
            status=ORDER_STATUS_OPEN,
        )
        limit_order = self._build_order(
            account_id,
            contract,
            ORDER_TYPE_LIMIT,
            bracket_side,
            size,
            limit_price=limit_price,
            custom_tag="sim_bracket_tp",
            status=ORDER_STATUS_OPEN,
        )
        stop_order["synthetic"] = True
        limit_order["synthetic"] = True
        stop_order["parentOrderId"] = parent_order_id
        limit_order["parentOrderId"] = parent_order_id

        self._state["orders"].append(stop_order)
        self._state["orders"].append(limit_order)
        self._state["brackets"].append(
            {
                "accountId": account_id,
                "contractId": contract_id,
                "parentOrderId": parent_order_id,
                "stopOrderId": stop_order["id"],
                "limitOrderId": limit_order["id"],
            }
        )

    def _build_order(
        self,
        account_id: int,
        contract: ContractInfo,
        order_type: int,
        side: int,
        size: int,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        custom_tag: Optional[str] = None,
        status: int = ORDER_STATUS_OPEN,
    ) -> Dict:
        order_id = self._next_id("nextOrderId")
        now_ts = _utc_now_iso()
        return {
            "id": order_id,
            "accountId": account_id,
            "contractId": contract.contract_id,
            "symbolId": contract.symbol_id,
            "creationTimestamp": now_ts,
            "updateTimestamp": now_ts,
            "status": status,
            "type": order_type,
            "side": side,
            "size": size,
            "limitPrice": limit_price,
            "stopPrice": stop_price,
            "fillVolume": 0,
            "filledPrice": None,
            "customTag": custom_tag,
        }

    def _bracket_offset(self, tick_size: float, tick_value: float, key: str) -> float:
        usd = float(self._config.get(key, 0.0))
        if tick_value <= 0:
            return 0.0
        ticks = round(usd / tick_value)
        return ticks * tick_size

    def _market_fill_price(self, contract_id: str) -> float:
        symbol = self._symbol_from_contract(contract_id)
        bar = self._bar_loader.get_latest_bar(symbol, timeframes=["1m", "1"])
        if bar and bar.get("c") is not None:
            return _safe_float(bar.get("c"))
        return 0.0

    def _apply_fill(
        self,
        account_id: int,
        contract_id: str,
        side: int,
        size: int,
        price: float,
        order_id: int,
    ) -> None:
        position = self._find_position(account_id, contract_id)
        position_type = POSITION_TYPE_LONG if side == ORDER_SIDE_BID else POSITION_TYPE_SHORT

        self._create_trade(
            account_id,
            contract_id,
            price,
            None,
            side,
            size,
            order_id,
        )

        if not position:
            position = {
                "id": self._next_id("nextPositionId"),
                "accountId": account_id,
                "contractId": contract_id,
                "creationTimestamp": _utc_now_iso(),
                "type": position_type,
                "size": size,
                "averagePrice": price,
            }
            self._state["positions"].append(position)
            return

        if position["type"] == position_type:
            total_size = position["size"] + size
            position["averagePrice"] = (
                (position["averagePrice"] * position["size"]) + (price * size)
            ) / total_size
            position["size"] = total_size
        else:
            existing_size = position["size"]
            if size >= existing_size:
                self._close_position(position, price, size_override=existing_size, order_override=None)
                remaining = size - existing_size
                if remaining > 0:
                    new_position = {
                        "id": self._next_id("nextPositionId"),
                        "accountId": account_id,
                        "contractId": contract_id,
                        "creationTimestamp": _utc_now_iso(),
                        "type": position_type,
                        "size": remaining,
                        "averagePrice": price,
                    }
                    self._state["positions"].append(new_position)
            else:
                position["size"] -= size
                if position["size"] <= 0:
                    self._remove_position(position)

    def _close_position(
        self,
        position: Dict,
        exit_price: float,
        size_override: Optional[int] = None,
        order_override: Optional[Dict] = None,
    ) -> None:
        size = size_override or position["size"]
        if size <= 0:
            return
        account_id = position["accountId"]
        contract_id = position["contractId"]
        side = ORDER_SIDE_ASK if position["type"] == POSITION_TYPE_LONG else ORDER_SIDE_BID

        pnl = self._compute_pnl(position["type"], position["averagePrice"], exit_price, size, contract_id)
        self._create_trade(
            account_id,
            contract_id,
            exit_price,
            pnl,
            side,
            size,
            (order_override or {}).get("id"),
        )

        self._update_balance(account_id, pnl)
        position["size"] -= size
        if position["size"] <= 0:
            self._remove_position(position)
            self._cancel_brackets(account_id, contract_id)

    def _create_trade(
        self,
        account_id: int,
        contract_id: str,
        price: float,
        pnl: Optional[float],
        side: int,
        size: int,
        order_id: Optional[int],
    ) -> None:
        trade = {
            "id": self._next_id("nextTradeId"),
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
        self._state["trades"].append(trade)

    def _compute_pnl(
        self,
        position_type: int,
        entry_price: float,
        exit_price: float,
        size: int,
        contract_id: str,
    ) -> float:
        contract = self._contract_info(contract_id)
        ticks = (exit_price - entry_price) / contract.tick_size
        pnl = ticks * contract.tick_value * size
        if position_type == POSITION_TYPE_SHORT:
            pnl = -pnl
        return pnl

    def _update_balance(self, account_id: int, pnl: float) -> None:
        for acct in self._state["accounts"]:
            if acct.get("id") == account_id:
                acct["balance"] = _safe_float(acct.get("balance")) + pnl
                return

    def _cancel_brackets(self, account_id: int, contract_id: str) -> None:
        for order in self._state["orders"]:
            if (
                order.get("accountId") == account_id
                and order.get("contractId") == contract_id
                and order.get("synthetic")
                and order.get("status") == ORDER_STATUS_OPEN
            ):
                order["status"] = ORDER_STATUS_CANCELLED
                order["updateTimestamp"] = _utc_now_iso()

    def _resize_brackets(self, account_id: int, contract_id: str, size: int) -> None:
        for order in self._state["orders"]:
            if (
                order.get("accountId") == account_id
                and order.get("contractId") == contract_id
                and order.get("synthetic")
                and order.get("status") == ORDER_STATUS_OPEN
            ):
                order["size"] = size
                order["updateTimestamp"] = _utc_now_iso()

    def _find_position(self, account_id: int, contract_id: str) -> Optional[Dict]:
        for position in self._state["positions"]:
            if position.get("accountId") == account_id and position.get("contractId") == contract_id:
                return position
        return None

    def _remove_position(self, position: Dict) -> None:
        if position in self._state["positions"]:
            self._state["positions"].remove(position)

    def _next_id(self, key: str) -> int:
        value = int(self._state.get(key, 1))
        self._state[key] = value + 1
        return value

    def _public_order(self, order: Dict) -> Dict:
        return {
            "id": order.get("id"),
            "accountId": order.get("accountId"),
            "contractId": order.get("contractId"),
            "symbolId": order.get("symbolId"),
            "creationTimestamp": order.get("creationTimestamp"),
            "updateTimestamp": order.get("updateTimestamp"),
            "status": order.get("status"),
            "type": order.get("type"),
            "side": order.get("side"),
            "size": order.get("size"),
            "limitPrice": order.get("limitPrice"),
            "stopPrice": order.get("stopPrice"),
            "fillVolume": order.get("fillVolume"),
            "filledPrice": order.get("filledPrice"),
            "customTag": order.get("customTag"),
        }
