import csv
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import load_config

STATE_PATH = "/mnt/data/simbroker_state.json"
STATE_LOCK = threading.Lock()

ORDER_SIDE = {"Bid": 0, "Ask": 1}
ORDER_TYPE = {"Limit": 1, "Market": 2, "Stop": 4, "TrailingStop": 5, "JoinBid": 6, "JoinAsk": 7}
ORDER_STATUS = {"Open": 1, "Filled": 2, "Cancelled": 3, "Expired": 4, "Rejected": 5, "Pending": 6}
POSITION_TYPE = {"Long": 1, "Short": 2}


@dataclass
class SimConfig:
    broker_mode: str
    tick_size: float
    tick_value: float
    bracket_sl_usd: float
    bracket_tp_usd: float
    fill_policy: str
    start_balance: float


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        if value.isdigit():
            ts = int(value)
            if ts > 10_000_000_000:
                return datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _round_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return round(price / tick_size) * tick_size


def _load_config() -> SimConfig:
    config = load_config()
    broker_mode = str(os.getenv("BROKER_MODE", "live")).strip().lower()
    tick_size = float(os.getenv("SIM_TICK_SIZE", 0.25))
    tick_value = float(os.getenv("SIM_TICK_VALUE", 1.25))
    bracket_sl_usd = float(os.getenv("SIM_BRACKET_SL_USD", 30))
    bracket_tp_usd = float(os.getenv("SIM_BRACKET_TP_USD", 60))
    fill_policy = str(os.getenv("SIM_FILL_POLICY", "worst")).strip().lower()
    start_balance = float(os.getenv("SIM_START_BALANCE", 100000))
    if broker_mode not in {"live", "sim"}:
        broker_mode = "live"
    if fill_policy not in {"worst", "best"}:
        fill_policy = "worst"
    return SimConfig(
        broker_mode=broker_mode,
        tick_size=tick_size,
        tick_value=tick_value,
        bracket_sl_usd=bracket_sl_usd,
        bracket_tp_usd=bracket_tp_usd,
        fill_policy=fill_policy,
        start_balance=start_balance,
    )


def _default_state() -> Dict:
    config = load_config()
    accounts = []
    for name, account_id in config.get("ACCOUNTS", {}).items():
        accounts.append(
            {
                "id": int(account_id),
                "name": name,
                "balance": float(os.getenv("SIM_START_BALANCE", 100000)),
                "canTrade": True,
                "isVisible": True,
                "simulated": True,
            }
        )
    return {
        "nextOrderId": 1,
        "nextPositionId": 1,
        "nextTradeId": 1,
        "accounts": accounts,
        "orders": [],
        "positions": [],
        "trades": [],
        "brackets": {},
        "last_processed_bar_ts": {},
    }


def _load_state() -> Dict:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    if not os.path.exists(STATE_PATH):
        state = _default_state()
        _write_state(state)
        return state
    with open(STATE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_state(state: Dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix="simbroker_state_", suffix=".tmp", dir=os.path.dirname(STATE_PATH))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp_path, STATE_PATH)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


class CsvDataFeed:
    def __init__(self) -> None:
        self._bars: List[Dict] = []
        self._source_path: Optional[str] = None
        self._load_once()

    def _candidate_paths(self) -> List[str]:
        return [
            "/mnt/data/tv_datafeed_5m_rows (2).csv",
            "/mnt/data/tv_datafeed_15m_rows.csv",
            "/mnt/data/tv_datafeed_30m_rows.csv",
            "/mnt/data/tv_datafeed_1d_rows.csv",
        ]

    def _load_once(self) -> None:
        for path in self._candidate_paths():
            if os.path.exists(path):
                self._source_path = path
                break
        if not self._source_path:
            self._bars = []
            return
        with open(self._source_path, "r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            bars: List[Dict] = []
            for row in reader:
                ts = row.get("t") or row.get("time") or row.get("timestamp") or row.get("date")
                bar_time = _parse_timestamp(ts)
                if not bar_time:
                    continue
                try:
                    bars.append(
                        {
                            "t": bar_time,
                            "o": float(row.get("o") or row.get("open") or 0),
                            "h": float(row.get("h") or row.get("high") or 0),
                            "l": float(row.get("l") or row.get("low") or 0),
                            "c": float(row.get("c") or row.get("close") or 0),
                            "v": float(row.get("v") or row.get("volume") or 0),
                        }
                    )
                except Exception:
                    continue
            self._bars = sorted(bars, key=lambda b: b["t"])

    def latest_bar(self) -> Optional[Dict]:
        return self._bars[-1] if self._bars else None

    def bars_between(self, start: Optional[datetime], end: Optional[datetime]) -> List[Dict]:
        if not self._bars:
            return []
        result = []
        for bar in self._bars:
            if start and bar["t"] <= start:
                continue
            if end and bar["t"] > end:
                break
            result.append(bar)
        return result


class SimBroker:
    def __init__(self) -> None:
        self._config = _load_config()
        self._feed = CsvDataFeed()
        self._state = _load_state()

    def _save(self) -> None:
        _write_state(self._state)

    def _next_id(self, key: str) -> int:
        next_id = int(self._state.get(key, 1))
        self._state[key] = next_id + 1
        return next_id

    def _get_account(self, account_id: int) -> Optional[Dict]:
        for account in self._state.get("accounts", []):
            if int(account.get("id")) == int(account_id):
                return account
        return None

    def _get_position(self, account_id: int, contract_id: str) -> Optional[Dict]:
        for pos in self._state.get("positions", []):
            if (
                int(pos.get("accountId")) == int(account_id)
                and pos.get("contractId") == contract_id
                and pos.get("isOpen", True)
            ):
                return pos
        return None

    def _get_open_orders(self, account_id: int) -> List[Dict]:
        return [
            order
            for order in self._state.get("orders", [])
            if int(order.get("accountId")) == int(account_id) and order.get("status") == ORDER_STATUS["Open"]
        ]

    def _current_price(self) -> Optional[float]:
        bar = self._feed.latest_bar()
        if bar:
            return bar.get("c")
        return None

    def _current_bar_time(self) -> Optional[datetime]:
        bar = self._feed.latest_bar()
        return bar.get("t") if bar else None

    def _compute_brackets(self, entry_price: float, position_type: int) -> Dict[str, float]:
        tick_value = self._config.tick_value
        tick_size = self._config.tick_size
        sl_ticks = round(self._config.bracket_sl_usd / tick_value) if tick_value else 0
        tp_ticks = round(self._config.bracket_tp_usd / tick_value) if tick_value else 0
        sl_offset = sl_ticks * tick_size
        tp_offset = tp_ticks * tick_size
        if position_type == POSITION_TYPE["Long"]:
            sl_price = entry_price - sl_offset
            tp_price = entry_price + tp_offset
        else:
            sl_price = entry_price + sl_offset
            tp_price = entry_price - tp_offset
        return {
            "sl": _round_to_tick(sl_price, tick_size),
            "tp": _round_to_tick(tp_price, tick_size),
        }

    def _append_trade(
        self,
        account_id: int,
        contract_id: str,
        price: float,
        size: int,
        side: int,
        order_id: int,
        profit_and_loss: Optional[float] = None,
    ) -> Dict:
        trade_id = self._next_id("nextTradeId")
        trade = {
            "id": trade_id,
            "accountId": account_id,
            "contractId": contract_id,
            "creationTimestamp": _now_iso(),
            "price": price,
            "profitAndLoss": profit_and_loss,
            "fees": 0.0,
            "side": side,
            "size": size,
            "voided": False,
            "orderId": order_id,
        }
        self._state.setdefault("trades", []).append(trade)
        return trade

    def _append_order(self, order: Dict) -> Dict:
        self._state.setdefault("orders", []).append(order)
        return order

    def _append_position(self, position: Dict) -> Dict:
        self._state.setdefault("positions", []).append(position)
        return position

    def _update_last_processed(self, account_id: int, contract_id: str, bar_time: datetime) -> None:
        key = f"{account_id}:{contract_id}"
        self._state.setdefault("last_processed_bar_ts", {})[key] = bar_time.isoformat()

    def sim_update(self, account_id: int, contract_id: str, now_ts_iso: Optional[str] = None) -> None:
        if not self._feed.latest_bar():
            return
        key = f"{account_id}:{contract_id}"
        last_ts_iso = self._state.get("last_processed_bar_ts", {}).get(key)
        last_ts = _parse_timestamp(last_ts_iso) if last_ts_iso else None
        end_ts = _parse_timestamp(now_ts_iso) if now_ts_iso else None
        bars = self._feed.bars_between(last_ts, end_ts)
        if not bars:
            return
        position = self._get_position(account_id, contract_id)
        if not position:
            self._update_last_processed(account_id, contract_id, bars[-1]["t"])
            self._save()
            return
        bracket_info = position.get("bracketOrderIds") or {}
        sl_id = bracket_info.get("stop")
        tp_id = bracket_info.get("limit")
        sl_order = next((o for o in self._state.get("orders", []) if o.get("id") == sl_id), None)
        tp_order = next((o for o in self._state.get("orders", []) if o.get("id") == tp_id), None)
        sl_price = sl_order.get("stopPrice") if sl_order else None
        tp_price = tp_order.get("limitPrice") if tp_order else None
        position_type = position.get("type")
        exit_side = ORDER_SIDE["Ask"] if position_type == POSITION_TYPE["Long"] else ORDER_SIDE["Bid"]

        for bar in bars:
            trigger = None
            if position_type == POSITION_TYPE["Long"]:
                sl_hit = sl_price is not None and bar["l"] <= sl_price
                tp_hit = tp_price is not None and bar["h"] >= tp_price
            else:
                sl_hit = sl_price is not None and bar["h"] >= sl_price
                tp_hit = tp_price is not None and bar["l"] <= tp_price
            if sl_hit or tp_hit:
                if sl_hit and tp_hit:
                    trigger = "sl" if self._config.fill_policy == "worst" else "tp"
                elif sl_hit:
                    trigger = "sl"
                else:
                    trigger = "tp"

                exit_price = sl_price if trigger == "sl" else tp_price
                self._close_position(
                    account_id,
                    contract_id,
                    exit_price,
                    exit_side,
                    triggered_order_id=sl_id if trigger == "sl" else tp_id,
                    cancel_order_id=tp_id if trigger == "sl" else sl_id,
                )
                self._update_last_processed(account_id, contract_id, bar["t"])
                break
        else:
            self._update_last_processed(account_id, contract_id, bars[-1]["t"])

    def sim_update_for_account(self, account_id: int) -> None:
        open_positions = [
            pos
            for pos in self._state.get("positions", [])
            if int(pos.get("accountId")) == int(account_id) and pos.get("isOpen", True)
        ]
        for pos in open_positions:
            self.sim_update(account_id, pos.get("contractId"))

    def auth_login_key(self, payload: Dict) -> Dict:
        return {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "token": "sim-token",
            "newToken": "sim-token",
        }

    def auth_validate(self, payload: Dict) -> Dict:
        return {"success": True, "errorCode": 0, "errorMessage": None}

    def account_search(self, payload: Dict) -> Dict:
        accounts = self._state.get("accounts", [])
        return {"success": True, "errorCode": 0, "errorMessage": None, "accounts": accounts}

    def contract_available(self, payload: Dict) -> Dict:
        return {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "contracts": [self._build_contract(payload.get("contractId") or "CON.F.US.MES.H26")],
        }

    def contract_search(self, payload: Dict) -> Dict:
        return {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "contracts": [self._build_contract(payload.get("contractId") or "CON.F.US.MES.H26")],
        }

    def contract_search_by_id(self, payload: Dict) -> Dict:
        contract_id = payload.get("contractId") or "CON.F.US.MES.H26"
        return {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "contract": self._build_contract(contract_id),
        }

    def _build_contract(self, contract_id: str) -> Dict:
        tick_size = self._config.tick_size
        tick_value = self._config.tick_value
        if "MES" in contract_id:
            tick_size = 0.25
            tick_value = 1.25
        return {
            "id": contract_id,
            "contractId": contract_id,
            "symbolId": contract_id,
            "name": contract_id,
            "tickSize": tick_size,
            "tickValue": tick_value,
        }

    def order_place(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        contract_id = payload.get("contractId")
        order_type = int(payload.get("type"))
        side = int(payload.get("side"))
        size = int(payload.get("size"))
        now = _now_iso()
        order_id = self._next_id("nextOrderId")
        order = {
            "id": order_id,
            "accountId": account_id,
            "contractId": contract_id,
            "symbolId": contract_id,
            "creationTimestamp": now,
            "updateTimestamp": now,
            "status": ORDER_STATUS["Open"],
            "type": order_type,
            "side": side,
            "size": size,
            "limitPrice": payload.get("limitPrice"),
            "stopPrice": payload.get("stopPrice"),
            "filledPrice": None,
            "fillVolume": 0,
            "customTag": payload.get("customTag"),
        }

        if order_type == ORDER_TYPE["Market"]:
            fill_price = self._current_price()
            if fill_price is not None:
                order["status"] = ORDER_STATUS["Filled"]
                order["filledPrice"] = fill_price
                order["fillVolume"] = size
            order["updateTimestamp"] = _now_iso()
        self._append_order(order)

        response = {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "orderId": order_id,
            "fillPrice": order.get("filledPrice"),
        }

        if order_type == ORDER_TYPE["Market"] and order.get("status") == ORDER_STATUS["Filled"]:
            position_type = POSITION_TYPE["Long"] if side == ORDER_SIDE["Bid"] else POSITION_TYPE["Short"]
            position = self._get_position(account_id, contract_id)
            if position and position.get("type") == position_type:
                new_total = position["size"] + size
                position["averagePrice"] = (
                    (position["averagePrice"] * position["size"]) + (fill_price * size)
                ) / new_total
                position["size"] = new_total
                position["updateTimestamp"] = _now_iso()
            else:
                position_id = self._next_id("nextPositionId")
                position = {
                    "id": position_id,
                    "accountId": account_id,
                    "contractId": contract_id,
                    "creationTimestamp": now,
                    "type": position_type,
                    "size": size,
                    "averagePrice": fill_price,
                    "isOpen": True,
                }
                self._append_position(position)

            self._append_trade(
                account_id,
                contract_id,
                price=fill_price,
                size=size,
                side=side,
                order_id=order_id,
                profit_and_loss=None,
            )

            bracket_prices = self._compute_brackets(fill_price, position_type)
            stop_order_id = self._next_id("nextOrderId")
            limit_order_id = self._next_id("nextOrderId")
            stop_order = {
                "id": stop_order_id,
                "accountId": account_id,
                "contractId": contract_id,
                "symbolId": contract_id,
                "creationTimestamp": now,
                "updateTimestamp": now,
                "status": ORDER_STATUS["Open"],
                "type": ORDER_TYPE["Stop"],
                "side": ORDER_SIDE["Ask"] if position_type == POSITION_TYPE["Long"] else ORDER_SIDE["Bid"],
                "size": position.get("size"),
                "limitPrice": None,
                "stopPrice": bracket_prices["sl"],
                "filledPrice": None,
                "fillVolume": 0,
                "customTag": "sim_bracket_sl",
            }
            limit_order = {
                "id": limit_order_id,
                "accountId": account_id,
                "contractId": contract_id,
                "symbolId": contract_id,
                "creationTimestamp": now,
                "updateTimestamp": now,
                "status": ORDER_STATUS["Open"],
                "type": ORDER_TYPE["Limit"],
                "side": ORDER_SIDE["Ask"] if position_type == POSITION_TYPE["Long"] else ORDER_SIDE["Bid"],
                "size": position.get("size"),
                "limitPrice": bracket_prices["tp"],
                "stopPrice": None,
                "filledPrice": None,
                "fillVolume": 0,
                "customTag": "sim_bracket_tp",
            }
            self._append_order(stop_order)
            self._append_order(limit_order)
            position["bracketOrderIds"] = {"stop": stop_order_id, "limit": limit_order_id}
            self._state.setdefault("brackets", {})[str(order_id)] = {
                "parentOrderId": order_id,
                "stopOrderId": stop_order_id,
                "limitOrderId": limit_order_id,
                "accountId": account_id,
                "contractId": contract_id,
                "positionId": position["id"],
            }

            bar_time = self._current_bar_time()
            if bar_time:
                self._update_last_processed(account_id, contract_id, bar_time)

        self._save()
        return response

    def order_search(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        start = _parse_timestamp(payload.get("startTimestamp"))
        end = _parse_timestamp(payload.get("endTimestamp"))
        orders = []
        for order in self._state.get("orders", []):
            if int(order.get("accountId")) != account_id:
                continue
            created = _parse_timestamp(order.get("creationTimestamp"))
            if start and (not created or created < start):
                continue
            if end and (not created or created > end):
                continue
            orders.append(order)
        return {"success": True, "errorCode": 0, "errorMessage": None, "orders": orders}

    def order_search_open(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        orders = self._get_open_orders(account_id)
        return {"success": True, "errorCode": 0, "errorMessage": None, "orders": orders}

    def order_cancel(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        order_id = int(payload.get("orderId"))
        for order in self._state.get("orders", []):
            if int(order.get("id")) == order_id and int(order.get("accountId")) == account_id:
                if order.get("status") == ORDER_STATUS["Open"]:
                    order["status"] = ORDER_STATUS["Cancelled"]
                    order["updateTimestamp"] = _now_iso()
                break
        self._save()
        return {"success": True, "errorCode": 0, "errorMessage": None}

    def order_modify(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        order_id = int(payload.get("orderId"))
        for order in self._state.get("orders", []):
            if int(order.get("id")) == order_id and int(order.get("accountId")) == account_id:
                if order.get("status") == ORDER_STATUS["Open"]:
                    if "size" in payload:
                        order["size"] = int(payload.get("size"))
                    if "limitPrice" in payload:
                        order["limitPrice"] = payload.get("limitPrice")
                    if "stopPrice" in payload:
                        order["stopPrice"] = payload.get("stopPrice")
                    order["updateTimestamp"] = _now_iso()
                break
        self._save()
        return {"success": True, "errorCode": 0, "errorMessage": None}

    def position_search_open(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        positions = [
            pos
            for pos in self._state.get("positions", [])
            if int(pos.get("accountId")) == account_id and pos.get("isOpen", True)
        ]
        return {"success": True, "errorCode": 0, "errorMessage": None, "positions": positions}

    def _close_position(
        self,
        account_id: int,
        contract_id: str,
        exit_price: float,
        exit_side: int,
        triggered_order_id: Optional[int] = None,
        cancel_order_id: Optional[int] = None,
    ) -> None:
        position = self._get_position(account_id, contract_id)
        if not position:
            return
        size = position.get("size", 0)
        entry_price = position.get("averagePrice")
        tick_size = self._config.tick_size
        tick_value = self._config.tick_value
        tick_diff = (exit_price - entry_price) / tick_size if tick_size else 0
        pnl = tick_diff * tick_value * size
        if position.get("type") == POSITION_TYPE["Short"]:
            pnl = -pnl

        order_id = triggered_order_id or self._next_id("nextOrderId")
        if triggered_order_id is None:
            now = _now_iso()
            order = {
                "id": order_id,
                "accountId": account_id,
                "contractId": contract_id,
                "symbolId": contract_id,
                "creationTimestamp": now,
                "updateTimestamp": now,
                "status": ORDER_STATUS["Filled"],
                "type": ORDER_TYPE["Market"],
                "side": exit_side,
                "size": size,
                "limitPrice": None,
                "stopPrice": None,
                "filledPrice": exit_price,
                "fillVolume": size,
                "customTag": "sim_close",
            }
            self._append_order(order)
        else:
            for order in self._state.get("orders", []):
                if order.get("id") == triggered_order_id:
                    order["status"] = ORDER_STATUS["Filled"]
                    order["filledPrice"] = exit_price
                    order["fillVolume"] = size
                    order["updateTimestamp"] = _now_iso()
                    break
        if cancel_order_id is not None:
            for order in self._state.get("orders", []):
                if order.get("id") == cancel_order_id and order.get("status") == ORDER_STATUS["Open"]:
                    order["status"] = ORDER_STATUS["Cancelled"]
                    order["updateTimestamp"] = _now_iso()
                    break

        position["isOpen"] = False
        position["size"] = 0
        position["updateTimestamp"] = _now_iso()

        self._append_trade(
            account_id,
            contract_id,
            price=exit_price,
            size=size,
            side=exit_side,
            order_id=order_id,
            profit_and_loss=round(pnl, 2),
        )
        account = self._get_account(account_id)
        if account is not None:
            account["balance"] = float(account.get("balance", 0)) + pnl

    def position_close_contract(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        contract_id = payload.get("contractId")
        price = self._current_price()
        if price is None:
            return {"success": False, "errorCode": 1, "errorMessage": "No market data"}
        position = self._get_position(account_id, contract_id)
        if not position:
            return {"success": True, "errorCode": 0, "errorMessage": None}
        exit_side = ORDER_SIDE["Ask"] if position.get("type") == POSITION_TYPE["Long"] else ORDER_SIDE["Bid"]
        bracket_ids = position.get("bracketOrderIds") or {}
        self._close_position(
            account_id,
            contract_id,
            price,
            exit_side,
            triggered_order_id=None,
            cancel_order_id=None,
        )
        for order_id in bracket_ids.values():
            for order in self._state.get("orders", []):
                if order.get("id") == order_id and order.get("status") == ORDER_STATUS["Open"]:
                    order["status"] = ORDER_STATUS["Cancelled"]
                    order["updateTimestamp"] = _now_iso()
        self._save()
        return {"success": True, "errorCode": 0, "errorMessage": None}

    def position_partial_close_contract(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        contract_id = payload.get("contractId")
        size = int(payload.get("size"))
        position = self._get_position(account_id, contract_id)
        if not position or size <= 0:
            return {"success": True, "errorCode": 0, "errorMessage": None}
        if size >= position.get("size", 0):
            return self.position_close_contract(payload)
        price = self._current_price()
        if price is None:
            return {"success": False, "errorCode": 1, "errorMessage": "No market data"}
        exit_side = ORDER_SIDE["Ask"] if position.get("type") == POSITION_TYPE["Long"] else ORDER_SIDE["Bid"]
        tick_size = self._config.tick_size
        tick_value = self._config.tick_value
        tick_diff = (price - position.get("averagePrice")) / tick_size if tick_size else 0
        pnl = tick_diff * tick_value * size
        if position.get("type") == POSITION_TYPE["Short"]:
            pnl = -pnl

        order_id = self._next_id("nextOrderId")
        order = {
            "id": order_id,
            "accountId": account_id,
            "contractId": contract_id,
            "symbolId": contract_id,
            "creationTimestamp": _now_iso(),
            "updateTimestamp": _now_iso(),
            "status": ORDER_STATUS["Filled"],
            "type": ORDER_TYPE["Market"],
            "side": exit_side,
            "size": size,
            "limitPrice": None,
            "stopPrice": None,
            "filledPrice": price,
            "fillVolume": size,
            "customTag": "sim_partial_close",
        }
        self._append_order(order)
        self._append_trade(
            account_id,
            contract_id,
            price=price,
            size=size,
            side=exit_side,
            order_id=order_id,
            profit_and_loss=round(pnl, 2),
        )
        position["size"] = position.get("size", 0) - size
        position["updateTimestamp"] = _now_iso()
        bracket_ids = position.get("bracketOrderIds") or {}
        for order_id in bracket_ids.values():
            for order in self._state.get("orders", []):
                if order.get("id") == order_id and order.get("status") == ORDER_STATUS["Open"]:
                    order["size"] = position["size"]
                    order["updateTimestamp"] = _now_iso()
        account = self._get_account(account_id)
        if account is not None:
            account["balance"] = float(account.get("balance", 0)) + pnl
        self._save()
        return {"success": True, "errorCode": 0, "errorMessage": None}

    def trade_search(self, payload: Dict) -> Dict:
        account_id = int(payload.get("accountId"))
        start = _parse_timestamp(payload.get("startTimestamp"))
        end = _parse_timestamp(payload.get("endTimestamp"))
        trades = []
        for trade in self._state.get("trades", []):
            if int(trade.get("accountId")) != account_id:
                continue
            created = _parse_timestamp(trade.get("creationTimestamp"))
            if start and (not created or created < start):
                continue
            if end and (not created or created > end):
                continue
            trades.append(trade)
        return {"success": True, "errorCode": 0, "errorMessage": None, "trades": trades}

    def history_retrieve_bars(self, payload: Dict) -> Dict:
        start = _parse_timestamp(payload.get("startTime"))
        end = _parse_timestamp(payload.get("endTime"))
        limit = payload.get("limit")
        bars = self._feed.bars_between(start, end)
        if limit:
            try:
                limit = int(limit)
                if limit > 0:
                    bars = bars[-limit:]
            except Exception:
                pass
        return {
            "success": True,
            "errorCode": 0,
            "errorMessage": None,
            "bars": [
                {
                    "t": int(bar["t"].timestamp() * 1000),
                    "o": bar["o"],
                    "h": bar["h"],
                    "l": bar["l"],
                    "c": bar["c"],
                    "v": bar["v"],
                }
                for bar in bars
            ],
        }


_BROKER: Optional[SimBroker] = None


def get_broker() -> SimBroker:
    global _BROKER
    if _BROKER is None:
        _BROKER = SimBroker()
    return _BROKER


def handle_post(path: str, payload: Dict) -> Dict:
    broker = get_broker()
    routes = {
        "/api/Auth/loginKey": broker.auth_login_key,
        "/api/Auth/validate": broker.auth_validate,
        "/api/Account/search": broker.account_search,
        "/api/Contract/available": broker.contract_available,
        "/api/Contract/search": broker.contract_search,
        "/api/Contract/searchById": broker.contract_search_by_id,
        "/api/Order/place": broker.order_place,
        "/api/Order/search": broker.order_search,
        "/api/Order/searchOpen": broker.order_search_open,
        "/api/Order/cancel": broker.order_cancel,
        "/api/Order/modify": broker.order_modify,
        "/api/Position/searchOpen": broker.position_search_open,
        "/api/Position/closeContract": broker.position_close_contract,
        "/api/Position/partialCloseContract": broker.position_partial_close_contract,
        "/api/Trade/search": broker.trade_search,
        "/api/History/retrieveBars": broker.history_retrieve_bars,
    }
    handler = routes.get(path)
    if not handler:
        return {"success": False, "errorCode": 404, "errorMessage": f"Unknown path: {path}"}
    with STATE_LOCK:
        broker._state = _load_state()
        account_id = payload.get("accountId") if isinstance(payload, dict) else None
        if account_id is not None:
            broker.sim_update_for_account(account_id)
        response = handler(payload or {})
        broker._save()
    return response
