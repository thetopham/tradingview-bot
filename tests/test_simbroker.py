from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from simbroker import InMemoryPriceFeed, SimBroker


ACCOUNT_ID = 101
CONTRACT_ID = "CON.F.US.MES.H26"
SYMBOL = "MES"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _make_config(accounts=None, bracket_overrides=None) -> dict:
    accounts = accounts or {"Sim": ACCOUNT_ID}
    bracket_overrides = bracket_overrides or {}
    return {
        "ACCOUNTS": accounts,
        "SIM_STARTING_BALANCE": 1000.0,
        "SIM_BRACKET_SL_USD": 1.0,
        "SIM_BRACKET_TP_USD": 2.0,
        "SIM_DEFAULT_TICK_SIZE": 1.0,
        "SIM_DEFAULT_TICK_VALUE": 1.0,
        "SIM_ACCOUNT_BRACKETS": bracket_overrides,
    }


def _make_broker(tmp_path, bars, config=None):
    state_path = tmp_path / "sim_state.json"
    price_feed = InMemoryPriceFeed({SYMBOL: {"1m": bars}})
    return SimBroker(str(state_path), config or _make_config(), price_feed=price_feed), price_feed


def _place_market_order(broker: SimBroker, payload_overrides=None) -> dict:
    payload = {
        "accountId": ACCOUNT_ID,
        "contractId": CONTRACT_ID,
        "type": 2,
        "side": 0,
        "size": 1,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return broker.handle_post("/api/Order/place", payload)


def test_place_market_returns_projectx_response(tmp_path):
    now = datetime.now(timezone.utc)
    bars = [
        {"t": _iso(now), "o": 100, "h": 100, "l": 100, "c": 100, "v": 1},
    ]
    broker, _ = _make_broker(tmp_path, bars)

    response = _place_market_order(broker)

    assert response["success"] is True
    assert isinstance(response["orderId"], int)


def test_bracket_children_appear_in_search_open(tmp_path):
    now = datetime.now(timezone.utc)
    bars = [
        {"t": _iso(now), "o": 100, "h": 100, "l": 100, "c": 100, "v": 1},
    ]
    broker, _ = _make_broker(tmp_path, bars)
    _place_market_order(broker)

    open_orders = broker.handle_post("/api/Order/searchOpen", {"accountId": ACCOUNT_ID})

    orders = open_orders["orders"]
    assert len(orders) == 2
    types = sorted(order["type"] for order in orders)
    assert types == [1, 4]


def test_tp_triggers_and_closes_position(tmp_path):
    now = datetime.now(timezone.utc)
    entry_bar = {"t": _iso(now), "o": 100, "h": 100, "l": 100, "c": 100, "v": 1}
    bars = [entry_bar]
    broker, _ = _make_broker(tmp_path, bars)
    _place_market_order(broker)

    next_bar_time = now + timedelta(minutes=1)
    bars.append({"t": _iso(next_bar_time), "o": 101, "h": 102, "l": 100.5, "c": 101.5, "v": 1})

    broker.sim_update(ACCOUNT_ID, CONTRACT_ID, _iso(next_bar_time))

    positions = broker.handle_post("/api/Position/searchOpen", {"accountId": ACCOUNT_ID})
    assert positions["positions"] == []

    orders = broker.handle_post("/api/Order/search", {"accountId": ACCOUNT_ID})["orders"]
    child_orders = [order for order in orders if order.get("parentOrderId") is not None]
    statuses = {order["status"] for order in child_orders}
    assert statuses == {2, 3}
    tp_order = next(order for order in child_orders if order["type"] == 1)
    sl_order = next(order for order in child_orders if order["type"] == 4)
    assert tp_order["status"] == 2
    assert sl_order["status"] == 3

    trades = broker.handle_post("/api/Trade/search", {"accountId": ACCOUNT_ID})["trades"]
    assert len(trades) == 2
    pnls = [trade["profitAndLoss"] for trade in trades]
    assert pnls.count(None) == 1
    assert any(pnl is not None for pnl in pnls)


def test_persists_state_across_restart(tmp_path):
    now = datetime.now(timezone.utc)
    entry_bar = {"t": _iso(now), "o": 100, "h": 100, "l": 100, "c": 100, "v": 1}
    bars = [entry_bar]
    broker, price_feed = _make_broker(tmp_path, bars)
    _place_market_order(broker)

    next_bar_time = now + timedelta(minutes=1)
    bars.append({"t": _iso(next_bar_time), "o": 101, "h": 102, "l": 100.5, "c": 101.5, "v": 1})
    broker.sim_update(ACCOUNT_ID, CONTRACT_ID, _iso(next_bar_time))

    restarted = SimBroker(str(tmp_path / "sim_state.json"), _make_config(), price_feed=price_feed)

    accounts = restarted.handle_post("/api/Account/search", {"onlyActiveAccounts": True})["accounts"]
    assert accounts[0]["balance"] == 1002.0

    orders = restarted.handle_post("/api/Order/search", {"accountId": ACCOUNT_ID})["orders"]
    trades = restarted.handle_post("/api/Trade/search", {"accountId": ACCOUNT_ID})["trades"]
    assert len(orders) == 3
    assert len(trades) == 2


def test_account_bracket_overrides_and_payload_ticks(tmp_path):
    now = datetime.now(timezone.utc)
    bars = [
        {"t": _iso(now), "o": 100, "h": 100, "l": 100, "c": 100, "v": 1},
    ]
    alt_account_id = 202
    config = _make_config(
        accounts={"Sim": ACCOUNT_ID, "Alt": alt_account_id},
        bracket_overrides={
            "sim": {"sl_usd": 1.0, "tp_usd": 2.0},
            "alt": {"sl_usd": 3.0, "tp_usd": 6.0},
        },
    )
    broker, _ = _make_broker(tmp_path, bars, config=config)

    broker.handle_post(
        "/api/Order/place",
        {
            "accountId": alt_account_id,
            "contractId": CONTRACT_ID,
            "type": 2,
            "side": 0,
            "size": 1,
        },
    )
    open_orders_alt = broker.handle_post("/api/Order/searchOpen", {"accountId": alt_account_id})["orders"]
    stop_order = next(order for order in open_orders_alt if order["type"] == 4)
    limit_order = next(order for order in open_orders_alt if order["type"] == 1)
    assert stop_order["stopPrice"] == 97
    assert limit_order["limitPrice"] == 106

    _place_market_order(
        broker,
        payload_overrides={
            "stopLossBracket": {"ticks": 5, "type": 4},
            "takeProfitBracket": {"ticks": 9, "type": 1},
        },
    )
    open_orders = broker.handle_post("/api/Order/searchOpen", {"accountId": ACCOUNT_ID})["orders"]
    stop_order = next(order for order in open_orders if order["type"] == 4)
    limit_order = next(order for order in open_orders if order["type"] == 1)
    assert stop_order["stopPrice"] == 95
    assert limit_order["limitPrice"] == 109
