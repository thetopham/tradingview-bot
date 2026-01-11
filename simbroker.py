import csv
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from dateutil import parser

from config import load_config

CONFIG = load_config()

STATE_PATH = Path("/mnt/data/simbroker_state.json")
DATA_PATHS = [
    "/mnt/data/tv_datafeed_5m_rows (2).csv",
    "/mnt/data/tv_datafeed_5m_rows.csv",
    "/mnt/data/tv_datafeed_15m_rows.csv",
    "/mnt/data/tv_datafeed_30m_rows.csv",
    "/mnt/data/tv_datafeed_1d_rows.csv",
]

ORDER_SIDE_BID = 0
ORDER_SIDE_ASK = 1

ORDER_TYPE_LIMIT = 1
ORDER_TYPE_MARKET = 2
ORDER_TYPE_STOP = 4
ORDER_TYPE_TRAILING_STOP = 5
ORDER_TYPE_JOIN_BID = 6
ORDER_TYPE_JOIN_ASK = 7

ORDER_STATUS_OPEN = 1
ORDER_STATUS_FILLED = 2
ORDER_STATUS_CANCELLED = 3
ORDER_STATUS_EXPIRED = 4
ORDER_STATUS_REJECTED = 5
ORDER_STATUS_PENDING = 6

POSITION_TYPE_LONG = 1
POSITION_TYPE_SHORT = 2

_state_lock = threading.Lock()
_broker_instance = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:
                return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            if ts > 1e9:
                return datetime.fromtimestamp(ts, tz=timezone.utc)
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            if value.isdigit():
                return _parse_timestamp(int(value))
            return parser.parse(value).astimezone(timezone.utc)
    except Exception:
        return None
    return None


def _round_to_tick(price: float, tick_size: float) -> float:
    if not tick_size:
        return price
    return round(price / tick_size) * tick_size


class SimDataFeed:
    def __init__(self, data_paths: List[str]):
        self.data_paths = data_paths
        self._path = None
        self._bars = []
        self._mtime = None

    def _select_path(self) -> Optional[str]:
        for path in self.data_paths:
            if os.path.exists(path):
                return path
        return None

    def _load_bars(self) -> List[Dict]:
        path = self._select_path()
        if not path:
            self._bars = []
            self._path = None
            self._mtime = None
            return []

        mtime = os.path.getmtime(path)
        if self._bars and path == self._path and mtime == self._mtime:
            return self._bars

        bars: List[Dict] = []
        with open(path, "r", encoding="utf-8") as handle:
            sample = handle.readline()
            handle.seek(0)
            sample_fields = [field.strip() for field in sample.split(",")]
            has_header = any(any(ch.isalpha() for ch in field) for field in sample_fields)

            if has_header:
                reader = csv.DictReader(handle)
                for row in reader:
                    bar = self._parse_row_dict(row)
                    if bar:
                        bars.append(bar)
            else:
                reader = csv.reader(handle)
                for row in reader:
                    bar = self._parse_row_list(row)
                    if bar:
                        bars.append(bar)

        bars.sort(key=lambda b: b["_dt"])
        self._bars = bars
        self._path = path
        self._mtime = mtime
        return bars

    def _parse_row_dict(self, row: Dict[str, str]) -> Optional[Dict]:
        def _first(keys):
            for key in keys:
                value = row.get(key)
                if value not in (None, ""):
                    return value
            return None

        ts_value = _first(["t", "time", "timestamp", "ts", "date"])
        dt = _parse_timestamp(ts_value)
        if not dt:
            return None

        try:
            open_value = float(_first(["o", "open"]))
            high_value = float(_first(["h", "high"]))
            low_value = float(_first(["l", "low"]))
            close_value = float(_first(["c", "close"]))
            volume_value = _first(["v", "volume", "vol"])
            volume_value = float(volume_value) if volume_value is not None else 0.0
        except Exception:
            return None

        return {
            "t": dt.isoformat(),
            "o": open_value,
            "h": high_value,
            "l": low_value,
            "c": close_value,
            "v": volume_value,
            "_dt": dt,
        }

    def _parse_row_list(self, row: List[str]) -> Optional[Dict]:
        if len(row) < 5:
            return None
        dt = _parse_timestamp(row[0])
        if not dt:
            return None
        try:
            open_value = float(row[1])
            high_value = float(row[2])
            low_value = float(row[3])
            close_value = float(row[4])
            volume_value = float(row[5]) if len(row) > 5 and row[5] else 0.0
        except Exception:
            return None
        return {
            "t": dt.isoformat(),
            "o": open_value,
            "h": high_value,
            "l": low_value,
            "c": close_value,
            "v": volume_value,
            "_dt": dt,
        }

    def latest_bar(self) -> Optional[Dict]:
        bars = self._load_bars()
        return bars[-1] if bars else None

    def bars_between(self, start: Optional[datetime], end: Optional[datetime]) -> List[Dict]:
        bars = self._load_bars()
        if not bars:
            return []
        filtered = []
        for bar in bars:
            dt = bar["_dt"]
            if start and dt < start:
                continue
            if end and dt > end:
                continue
            filtered.append(bar)
        return filtered


class SimBroker:
    def __init__(self, state_path: Path = STATE_PATH):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_feed = SimDataFeed(DATA_PATHS)
        self.state = self._load_state()

    def _load_state(self) -> Dict:
        if self.state_path.exists():
            try:
                with self.state_path.open("r", encoding="utf-8") as handle:
                    state = json.load(handle)
            except Exception:
                state = {}
        else:
            state = {}

        state.setdefault("accounts", [])
        state.setdefault("orders", [])
        state.setdefault("positions", [])
        state.setdefault("trades", [])
        state.setdefault("synthetic_brackets", {})
        state.setdefault("last_processed_bar_ts", {})
        state.setdefault("nextOrderId", 1)
        state.setdefault("nextPositionId", 1)
        state.setdefault("nextTradeId", 1)

        existing_ids = {int(acc.get("id")) for acc in state["accounts"] if acc.get("id") is not None}
        for name, account_id in CONFIG.get("ACCOUNTS", {}).items():
            if int(account_id) not in existing_ids:
                state["accounts"].append(
                    {
                        "id": int(account_id),
                        "name": name.upper(),
                        "balance": 0.0,
                        "canTrade": True,
                        "isVisible": True,
                        "simulated": True,
                    }
                )

        max_order_id = max([o.get("id", 0) for o in state["orders"]], default=0)
        max_position_id = max([p.get("id", 0) for p in state["positions"]], default=0)
        max_trade_id = max([t.get("id", 0) for t in state["trades"]], default=0)
        state["nextOrderId"] = max(state["nextOrderId"], max_order_id + 1)
        state["nextPositionId"] = max(state["nextPositionId"], max_position_id + 1)
        state["nextTradeId"] = max(state["nextTradeId"], max_trade_id + 1)

        return state

    def _save_state(self) -> None:
        tmp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        with _state_lock:
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=2)
            os.replace(tmp_path, self.state_path)

    def _next_id(self, key: str) -> int:
        next_id = int(self.state.get(key, 1))
        self.state[key] = next_id + 1
        return next_id

    def _get_account(self, account_id: int) -> Optional[Dict]:
        for acct in self.state["accounts"]:
            if int(acct.get("id")) == int(account_id):
                return acct
        return None

    def _contract(self, contract_id: str) -> Dict:
        tick_size = float(CONFIG.get("SIM_TICK_SIZE", 0.25))
        tick_value = float(CONFIG.get("SIM_TICK_VALUE", 1.25))
        if contract_id and "MES" in contract_id:
            tick_size = float(CONFIG.get("SIM_TICK_SIZE", 0.25))
            tick_value = float(CONFIG.get("SIM_TICK_VALUE", 1.25))
        symbol_id = "F.US.MES" if contract_id and "MES" in contract_id else "F.US.SIM"
        return {
            "id": contract_id,
            "name": contract_id.split(".")[-1] if contract_id else "SIM",
            "description": f"Simulated contract {contract_id}",
            "tickSize": tick_size,
            "tickValue": tick_value,
            "activeContract": True,
            "symbolId": symbol_id,
        }

    def _current_price(self) -> Optional[float]:
        bar = self.data_feed.latest_bar()
        if not bar:
            return None
        return float(bar["c"])

    def _order_template(self, account_id: int, contract_id: str, order_type: int, side: int, size: int) -> Dict:
        now = _utc_now_iso()
        order_id = self._next_id("nextOrderId")
        return {
            "id": order_id,
            "orderId": order_id,
            "accountId": int(account_id),
            "contractId": contract_id,
            "symbolId": self._contract(contract_id)["symbolId"],
            "creationTimestamp": now,
            "updateTimestamp": now,
            "status": ORDER_STATUS_OPEN,
            "type": order_type,
            "side": side,
            "size": int(size),
            "limitPrice": None,
            "stopPrice": None,
            "filledPrice": None,
            "fillVolume": 0,
            "customTag": None,
        }

    def _create_trade(self, account_id: int, contract_id: str, price: float, side: int, size: int, order_id: int,
                      profit_and_loss: Optional[float]) -> Dict:
        trade_id = self._next_id("nextTradeId")
        trade = {
            "id": trade_id,
            "accountId": int(account_id),
            "contractId": contract_id,
            "creationTimestamp": _utc_now_iso(),
            "price": float(price),
            "profitAndLoss": profit_and_loss,
            "fees": 0.0,
            "side": side,
            "size": int(size),
            "voided": False,
            "orderId": order_id,
        }
        self.state["trades"].append(trade)
        return trade

    def _get_open_position(self, account_id: int, contract_id: str) -> Optional[Dict]:
        for pos in self.state["positions"]:
            if pos.get("closed"):
                continue
            if int(pos.get("accountId")) == int(account_id) and pos.get("contractId") == contract_id:
                return pos
        return None

    def _cancel_brackets(self, account_id: int, contract_id: str) -> None:
        for order in self.state["orders"]:
            if order.get("status") != ORDER_STATUS_OPEN:
                continue
            if int(order.get("accountId")) != int(account_id):
                continue
            if order.get("contractId") != contract_id:
                continue
            if not order.get("synthetic"):
                continue
            order["status"] = ORDER_STATUS_CANCELLED
            order["updateTimestamp"] = _utc_now_iso()

    def _sync_brackets(self, position: Dict, entry_price: float) -> None:
        account_id = position["accountId"]
        contract_id = position["contractId"]
        self._cancel_brackets(account_id, contract_id)

        sl_usd = float(CONFIG.get("SIM_BRACKET_SL_USD", 30))
        tp_usd = float(CONFIG.get("SIM_BRACKET_TP_USD", 60))
        tick_size = float(CONFIG.get("SIM_TICK_SIZE", 0.25))
        tick_value = float(CONFIG.get("SIM_TICK_VALUE", 1.25))
        ticks_sl = round(sl_usd / tick_value) if tick_value else 0
        ticks_tp = round(tp_usd / tick_value) if tick_value else 0
        offset_sl = ticks_sl * tick_size
        offset_tp = ticks_tp * tick_size

        position_type = position.get("type")
        if position_type == POSITION_TYPE_LONG:
            sl_price = entry_price - offset_sl
            tp_price = entry_price + offset_tp
            exit_side = ORDER_SIDE_ASK
        else:
            sl_price = entry_price + offset_sl
            tp_price = entry_price - offset_tp
            exit_side = ORDER_SIDE_BID

        sl_price = _round_to_tick(sl_price, tick_size)
        tp_price = _round_to_tick(tp_price, tick_size)

        for order_type, price, role in (
            (ORDER_TYPE_STOP, sl_price, "stop_loss"),
            (ORDER_TYPE_LIMIT, tp_price, "take_profit"),
        ):
            order = self._order_template(account_id, contract_id, order_type, exit_side, position["size"])
            if order_type == ORDER_TYPE_STOP:
                order["stopPrice"] = price
            else:
                order["limitPrice"] = price
            order["synthetic"] = True
            order["parentOrderId"] = position.get("entryOrderId")
            order["bracketRole"] = role
            self.state["orders"].append(order)

    def _position_point_value(self) -> float:
        tick_size = float(CONFIG.get("SIM_TICK_SIZE", 0.25))
        tick_value = float(CONFIG.get("SIM_TICK_VALUE", 1.25))
        if tick_size == 0:
            return 1.0
        return tick_value / tick_size

    def _apply_fill(self, account_id: int, contract_id: str, side: int, size: int, price: float, order_id: int) -> None:
        position = self._get_open_position(account_id, contract_id)
        point_value = self._position_point_value()

        if position is None:
            position_id = self._next_id("nextPositionId")
            position_type = POSITION_TYPE_LONG if side == ORDER_SIDE_BID else POSITION_TYPE_SHORT
            new_pos = {
                "id": position_id,
                "accountId": int(account_id),
                "contractId": contract_id,
                "creationTimestamp": _utc_now_iso(),
                "type": position_type,
                "size": int(size),
                "averagePrice": float(price),
                "entryOrderId": order_id,
                "closed": False,
            }
            self.state["positions"].append(new_pos)
            self._create_trade(account_id, contract_id, price, side, size, order_id, None)
            self._sync_brackets(new_pos, new_pos["averagePrice"])
            return

        same_direction = (position["type"] == POSITION_TYPE_LONG and side == ORDER_SIDE_BID) or (
            position["type"] == POSITION_TYPE_SHORT and side == ORDER_SIDE_ASK
        )

        if same_direction:
            new_size = position["size"] + size
            if new_size:
                position["averagePrice"] = (
                    position["averagePrice"] * position["size"] + price * size
                ) / new_size
            position["size"] = new_size
            position["entryOrderId"] = order_id
            self._create_trade(account_id, contract_id, price, side, size, order_id, None)
            self._sync_brackets(position, position["averagePrice"])
            return

        closing_size = min(position["size"], size)
        remaining = size - closing_size
        if position["type"] == POSITION_TYPE_LONG:
            pnl = (price - position["averagePrice"]) * closing_size * point_value
        else:
            pnl = (position["averagePrice"] - price) * closing_size * point_value

        self._create_trade(account_id, contract_id, price, side, closing_size, order_id, pnl)

        account = self._get_account(account_id)
        if account is not None:
            account["balance"] = float(account.get("balance", 0.0)) + float(pnl)

        position["size"] -= closing_size
        if position["size"] <= 0:
            position["closed"] = True
            position["updateTimestamp"] = _utc_now_iso()
            self._cancel_brackets(account_id, contract_id)
        else:
            self._sync_brackets(position, position["averagePrice"])

        if remaining > 0:
            position_id = self._next_id("nextPositionId")
            new_type = POSITION_TYPE_LONG if side == ORDER_SIDE_BID else POSITION_TYPE_SHORT
            new_pos = {
                "id": position_id,
                "accountId": int(account_id),
                "contractId": contract_id,
                "creationTimestamp": _utc_now_iso(),
                "type": new_type,
                "size": int(remaining),
                "averagePrice": float(price),
                "entryOrderId": order_id,
                "closed": False,
            }
            self.state["positions"].append(new_pos)
            self._create_trade(account_id, contract_id, price, side, remaining, order_id, None)
            self._sync_brackets(new_pos, new_pos["averagePrice"])

    def _fill_market_order(self, order: Dict, price: float) -> None:
        order["status"] = ORDER_STATUS_FILLED
        order["filledPrice"] = float(price)
        order["fillVolume"] = int(order["size"])
        order["updateTimestamp"] = _utc_now_iso()
        self._apply_fill(order["accountId"], order["contractId"], order["side"], order["size"], price, order["id"])

    def sim_update(self, account_id: int, contract_id: Optional[str], now_ts_iso: Optional[str] = None) -> None:
        now_dt = _parse_timestamp(now_ts_iso) if now_ts_iso else None
        if now_dt is None:
            latest_bar = self.data_feed.latest_bar()
            now_dt = latest_bar["_dt"] if latest_bar else None
        if now_dt is None:
            return

        contracts = {contract_id} if contract_id else {
            pos.get("contractId") for pos in self.state["positions"] if not pos.get("closed") and int(pos.get("accountId")) == int(account_id)
        }
        contracts.discard(None)

        for cid in contracts:
            key = f"{account_id}:{cid}"
            last_ts = self.state["last_processed_bar_ts"].get(key)
            last_dt = _parse_timestamp(last_ts) if last_ts else None
            bars = self.data_feed.bars_between(last_dt, now_dt)
            if last_dt:
                bars = [b for b in bars if b["_dt"] > last_dt]

            for bar in bars:
                self._process_bar(account_id, cid, bar)
                self.state["last_processed_bar_ts"][key] = bar["t"]

        self._save_state()

    def _process_bar(self, account_id: int, contract_id: str, bar: Dict) -> None:
        position = self._get_open_position(account_id, contract_id)
        if not position:
            return

        open_orders = [
            o for o in self.state["orders"]
            if o.get("status") == ORDER_STATUS_OPEN
            and o.get("synthetic")
            and int(o.get("accountId")) == int(account_id)
            and o.get("contractId") == contract_id
        ]
        stop_order = next((o for o in open_orders if o.get("type") == ORDER_TYPE_STOP), None)
        limit_order = next((o for o in open_orders if o.get("type") == ORDER_TYPE_LIMIT), None)
        if not stop_order and not limit_order:
            return

        high = bar["h"]
        low = bar["l"]
        is_long = position.get("type") == POSITION_TYPE_LONG

        sl_hit = False
        tp_hit = False
        if stop_order:
            stop_price = stop_order.get("stopPrice")
            if stop_price is not None:
                sl_hit = low <= stop_price if is_long else high >= stop_price
        if limit_order:
            limit_price = limit_order.get("limitPrice")
            if limit_price is not None:
                tp_hit = high >= limit_price if is_long else low <= limit_price

        if not sl_hit and not tp_hit:
            return

        fill_policy = CONFIG.get("SIM_FILL_POLICY", "worst")
        exit_order = None
        if sl_hit and tp_hit:
            exit_order = stop_order if fill_policy == "worst" else limit_order
        else:
            exit_order = stop_order if sl_hit else limit_order

        if not exit_order:
            return

        fill_price = exit_order.get("stopPrice") if exit_order.get("type") == ORDER_TYPE_STOP else exit_order.get("limitPrice")
        if fill_price is None:
            return

        self._fill_exit_order(position, exit_order, float(fill_price))

    def _fill_exit_order(self, position: Dict, exit_order: Dict, price: float) -> None:
        account_id = position["accountId"]
        contract_id = position["contractId"]
        exit_order["status"] = ORDER_STATUS_FILLED
        exit_order["filledPrice"] = float(price)
        exit_order["fillVolume"] = int(exit_order["size"])
        exit_order["updateTimestamp"] = _utc_now_iso()

        other_orders = [
            o for o in self.state["orders"]
            if o.get("status") == ORDER_STATUS_OPEN
            and o.get("synthetic")
            and int(o.get("accountId")) == int(account_id)
            and o.get("contractId") == contract_id
            and o.get("id") != exit_order.get("id")
        ]
        for order in other_orders:
            order["status"] = ORDER_STATUS_CANCELLED
            order["updateTimestamp"] = _utc_now_iso()

        close_size = position["size"]
        point_value = self._position_point_value()
        if position.get("type") == POSITION_TYPE_LONG:
            pnl = (price - position["averagePrice"]) * close_size * point_value
        else:
            pnl = (position["averagePrice"] - price) * close_size * point_value

        self._create_trade(account_id, contract_id, price, exit_order["side"], close_size, exit_order["id"], pnl)

        account = self._get_account(account_id)
        if account is not None:
            account["balance"] = float(account.get("balance", 0.0)) + float(pnl)

        position["closed"] = True
        position["updateTimestamp"] = _utc_now_iso()

    def handle_request(self, path: str, payload: Dict) -> Dict:
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
            return {
                "accounts": self.state["accounts"],
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        if path in {"/api/Contract/available", "/api/Contract/search"}:
            contract_id = payload.get("contractId") or CONFIG.get("OVERRIDE_CONTRACT_ID", "CON.F.US.MES.H26")
            return {
                "contracts": [self._contract(contract_id)],
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        if path == "/api/Contract/searchById":
            contract_id = payload.get("contractId") or CONFIG.get("OVERRIDE_CONTRACT_ID", "CON.F.US.MES.H26")
            return {
                "contract": self._contract(contract_id),
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        if path == "/api/Order/place":
            account_id = payload.get("accountId")
            contract_id = payload.get("contractId")
            order_type = payload.get("type")
            side = payload.get("side")
            size = int(payload.get("size", 0))
            order = self._order_template(account_id, contract_id, order_type, side, size)
            order["limitPrice"] = payload.get("limitPrice")
            order["stopPrice"] = payload.get("stopPrice")
            order["customTag"] = payload.get("customTag")
            self.state["orders"].append(order)

            if order_type == ORDER_TYPE_MARKET:
                price = self._current_price()
                if price is None:
                    order["status"] = ORDER_STATUS_REJECTED
                    order["updateTimestamp"] = _utc_now_iso()
                    self._save_state()
                    return {
                        "orderId": order["id"],
                        "success": False,
                        "errorCode": 1,
                        "errorMessage": "No market data available",
                    }
                self._fill_market_order(order, price)

            self._save_state()
            response = {
                "orderId": order["id"],
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }
            if order.get("filledPrice") is not None:
                response["fillPrice"] = order.get("filledPrice")
            return response

        if path == "/api/Order/search":
            account_id = payload.get("accountId")
            start_dt = _parse_timestamp(payload.get("startTimestamp"))
            end_dt = _parse_timestamp(payload.get("endTimestamp"))
            orders = [
                o for o in self.state["orders"]
                if int(o.get("accountId")) == int(account_id)
            ]
            filtered = []
            for order in orders:
                ts = _parse_timestamp(order.get("creationTimestamp"))
                if start_dt and ts and ts < start_dt:
                    continue
                if end_dt and ts and ts > end_dt:
                    continue
                filtered.append(order)
            return {
                "orders": filtered,
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        if path == "/api/Order/searchOpen":
            account_id = payload.get("accountId")
            orders = [
                o for o in self.state["orders"]
                if int(o.get("accountId")) == int(account_id) and o.get("status") == ORDER_STATUS_OPEN
            ]
            return {
                "orders": orders,
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        if path == "/api/Order/cancel":
            account_id = payload.get("accountId")
            order_id = int(payload.get("orderId"))
            for order in self.state["orders"]:
                if int(order.get("accountId")) == int(account_id) and int(order.get("id")) == order_id:
                    if order.get("status") == ORDER_STATUS_OPEN:
                        order["status"] = ORDER_STATUS_CANCELLED
                        order["updateTimestamp"] = _utc_now_iso()
            self._save_state()
            return {
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        if path == "/api/Order/modify":
            account_id = payload.get("accountId")
            order_id = int(payload.get("orderId"))
            for order in self.state["orders"]:
                if int(order.get("accountId")) == int(account_id) and int(order.get("id")) == order_id:
                    if order.get("status") == ORDER_STATUS_OPEN:
                        if payload.get("size") is not None:
                            order["size"] = int(payload.get("size"))
                        if payload.get("limitPrice") is not None:
                            order["limitPrice"] = payload.get("limitPrice")
                        if payload.get("stopPrice") is not None:
                            order["stopPrice"] = payload.get("stopPrice")
                        order["updateTimestamp"] = _utc_now_iso()
            self._save_state()
            return {
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        if path == "/api/Position/searchOpen":
            account_id = payload.get("accountId")
            positions = [
                p for p in self.state["positions"]
                if not p.get("closed") and int(p.get("accountId")) == int(account_id)
            ]
            return {
                "positions": positions,
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        if path == "/api/Position/closeContract":
            account_id = payload.get("accountId")
            contract_id = payload.get("contractId")
            position = self._get_open_position(account_id, contract_id)
            if position:
                price = self._current_price() or position.get("averagePrice")
                order = self._order_template(account_id, contract_id, ORDER_TYPE_MARKET,
                                             ORDER_SIDE_ASK if position.get("type") == POSITION_TYPE_LONG else ORDER_SIDE_BID,
                                             position.get("size"))
                self.state["orders"].append(order)
                self._fill_exit_order(position, order, float(price))
            self._save_state()
            return {
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        if path == "/api/Position/partialCloseContract":
            account_id = payload.get("accountId")
            contract_id = payload.get("contractId")
            size = int(payload.get("size", 0))
            position = self._get_open_position(account_id, contract_id)
            if position and size > 0:
                price = self._current_price() or position.get("averagePrice")
                close_size = min(size, position.get("size"))
                order = self._order_template(account_id, contract_id, ORDER_TYPE_MARKET,
                                             ORDER_SIDE_ASK if position.get("type") == POSITION_TYPE_LONG else ORDER_SIDE_BID,
                                             close_size)
                self.state["orders"].append(order)
                order["status"] = ORDER_STATUS_FILLED
                order["filledPrice"] = float(price)
                order["fillVolume"] = close_size
                order["updateTimestamp"] = _utc_now_iso()

                point_value = self._position_point_value()
                if position.get("type") == POSITION_TYPE_LONG:
                    pnl = (price - position["averagePrice"]) * close_size * point_value
                else:
                    pnl = (position["averagePrice"] - price) * close_size * point_value

                self._create_trade(account_id, contract_id, price, order["side"], close_size, order["id"], pnl)
                account = self._get_account(account_id)
                if account is not None:
                    account["balance"] = float(account.get("balance", 0.0)) + float(pnl)

                position["size"] -= close_size
                if position["size"] <= 0:
                    position["closed"] = True
                    position["updateTimestamp"] = _utc_now_iso()
                    self._cancel_brackets(account_id, contract_id)
                else:
                    self._sync_brackets(position, position["averagePrice"])

            self._save_state()
            return {
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        if path == "/api/Trade/search":
            account_id = payload.get("accountId")
            start_dt = _parse_timestamp(payload.get("startTimestamp"))
            end_dt = _parse_timestamp(payload.get("endTimestamp"))
            trades = [
                t for t in self.state["trades"]
                if int(t.get("accountId")) == int(account_id)
            ]
            filtered = []
            for trade in trades:
                ts = _parse_timestamp(trade.get("creationTimestamp"))
                if start_dt and ts and ts < start_dt:
                    continue
                if end_dt and ts and ts > end_dt:
                    continue
                filtered.append(trade)
            return {
                "trades": filtered,
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        if path == "/api/History/retrieveBars":
            start_dt = _parse_timestamp(payload.get("startTime"))
            end_dt = _parse_timestamp(payload.get("endTime"))
            limit = payload.get("limit")
            bars = self.data_feed.bars_between(start_dt, end_dt)
            bars_sorted = sorted(bars, key=lambda b: b["_dt"], reverse=True)
            if limit:
                bars_sorted = bars_sorted[: int(limit)]
            payload_bars = [
                {"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
                for b in bars_sorted
            ]
            return {
                "bars": payload_bars,
                "success": True,
                "errorCode": 0,
                "errorMessage": None,
            }

        return {
            "success": False,
            "errorCode": 404,
            "errorMessage": f"Unsupported path {path}",
        }


def get_broker() -> SimBroker:
    global _broker_instance
    if _broker_instance is None:
        _broker_instance = SimBroker()
    return _broker_instance


def sim_update(account_id: int, contract_id: Optional[str], now_ts_iso: Optional[str] = None) -> None:
    broker = get_broker()
    broker.sim_update(account_id, contract_id, now_ts_iso=now_ts_iso)
