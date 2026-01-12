from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from simbroker import InMemoryPriceFeed, SimBroker


SIM001_ID = 20001
SIM002_ID = 20002
CONTRACT_ID = "CON.F.US.MES.H26"
SYMBOL = "MES"
TICK_SIZE = 0.25
TICK_VALUE = 1.25


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _make_sim_account_settings(risk_basis: str = "per_position") -> dict:
    return {
        SIM001_ID: {
            "name": "sim001",
            "balance": 50000.0,
            "sl_usd": 30.0,
            "tp_usd": 60.0,
            "risk_basis": risk_basis,
            "fill_policy": "worst",
        },
        SIM002_ID: {
            "name": "sim002",
            "balance": 50000.0,
            "sl_usd": 50.0,
            "tp_usd": 100.0,
            "risk_basis": risk_basis,
            "fill_policy": "worst",
        },
    }


def _make_broker(tmp_path, bars, risk_basis: str = "per_position"):
    state_path = tmp_path / "sim_state.json"
    price_feed = InMemoryPriceFeed({SYMBOL: {"1m": bars}})
    broker = SimBroker(
        str(state_path),
        config={"SIM_DEFAULT_TICK_SIZE": TICK_SIZE, "SIM_DEFAULT_TICK_VALUE": TICK_VALUE},
        sim_account_settings_by_id=_make_sim_account_settings(risk_basis=risk_basis),
        price_feed=price_feed,
    )
    return broker, price_feed


def _place_market_buy(broker: SimBroker, account_id: int) -> dict:
    return broker.handle_post(
        "/api/Order/place",
        {
            "accountId": account_id,
            "contractId": CONTRACT_ID,
            "type": 2,
            "side": 0,
            "size": 1,
        },
    )


def _expected_bracket_prices(entry_price: float, sl_usd: float, tp_usd: float, size: float, risk_basis: str):
    if risk_basis == "per_contract":
        sl_ticks = round(sl_usd / TICK_VALUE)
        tp_ticks = round(tp_usd / TICK_VALUE)
    else:
        sl_ticks = round(sl_usd / (TICK_VALUE * size))
        tp_ticks = round(tp_usd / (TICK_VALUE * size))
    return (
        entry_price - (sl_ticks * TICK_SIZE),
        entry_price + (tp_ticks * TICK_SIZE),
    )


def test_multi_account_brackets_and_open_orders(tmp_path):
    now = datetime.now(timezone.utc)
    entry_price = 100.0
    bars = [{"t": _iso(now), "o": entry_price, "h": entry_price, "l": entry_price, "c": entry_price, "v": 1}]
    risk_basis = "per_position"
    broker, _ = _make_broker(tmp_path, bars, risk_basis=risk_basis)

    response = _place_market_buy(broker, SIM001_ID)
    assert response["success"] is True
    assert isinstance(response["orderId"], int)

    open_orders_sim001 = broker.handle_post("/api/Order/searchOpen", {"accountId": SIM001_ID})["orders"]
    assert len(open_orders_sim001) == 2
    assert all(order["status"] == 1 for order in open_orders_sim001)
    stop_order = next(order for order in open_orders_sim001 if order["type"] == 4)
    limit_order = next(order for order in open_orders_sim001 if order["type"] == 1)
    expected_sl, expected_tp = _expected_bracket_prices(entry_price, 30.0, 60.0, 1, risk_basis)
    assert stop_order["stopPrice"] == expected_sl
    assert limit_order["limitPrice"] == expected_tp

    _place_market_buy(broker, SIM002_ID)
    open_orders_sim002 = broker.handle_post("/api/Order/searchOpen", {"accountId": SIM002_ID})["orders"]
    stop_sim002 = next(order for order in open_orders_sim002 if order["type"] == 4)
    limit_sim002 = next(order for order in open_orders_sim002 if order["type"] == 1)
    expected_sl_2, expected_tp_2 = _expected_bracket_prices(entry_price, 50.0, 100.0, 1, risk_basis)
    assert stop_sim002["stopPrice"] == expected_sl_2
    assert limit_sim002["limitPrice"] == expected_tp_2
    assert expected_sl != expected_sl_2
    assert expected_tp != expected_tp_2


def test_tp_hit_closes_position_and_updates_orders(tmp_path):
    now = datetime.now(timezone.utc)
    entry_price = 100.0
    entry_bar = {"t": _iso(now), "o": entry_price, "h": entry_price, "l": entry_price, "c": entry_price, "v": 1}
    bars = [entry_bar]
    broker, _ = _make_broker(tmp_path, bars)
    _place_market_buy(broker, SIM001_ID)

    next_bar_time = now + timedelta(minutes=1)
    _, expected_tp = _expected_bracket_prices(entry_price, 30.0, 60.0, 1, "per_position")
    bars.append(
        {
            "t": _iso(next_bar_time),
            "o": entry_price,
            "h": expected_tp,
            "l": entry_price - 0.25,
            "c": expected_tp,
            "v": 1,
        }
    )
    broker.sim_update(SIM001_ID, CONTRACT_ID, _iso(next_bar_time))

    positions = broker.handle_post("/api/Position/searchOpen", {"accountId": SIM001_ID})["positions"]
    assert positions == []

    orders = broker.handle_post("/api/Order/search", {"accountId": SIM001_ID})["orders"]
    child_orders = [order for order in orders if order.get("parentOrderId") is not None]
    tp_order = next(order for order in child_orders if order["type"] == 1)
    sl_order = next(order for order in child_orders if order["type"] == 4)
    assert tp_order["status"] == 2
    assert sl_order["status"] == 3

    trades = broker.handle_post("/api/Trade/search", {"accountId": SIM001_ID})["trades"]
    assert len(trades) == 2
    assert any(trade["profitAndLoss"] is not None for trade in trades)


def test_state_persists_across_broker_restart(tmp_path):
    now = datetime.now(timezone.utc)
    entry_price = 100.0
    bars = [{"t": _iso(now), "o": entry_price, "h": entry_price, "l": entry_price, "c": entry_price, "v": 1}]
    broker, price_feed = _make_broker(tmp_path, bars)
    _place_market_buy(broker, SIM001_ID)

    restarted = SimBroker(
        str(tmp_path / "sim_state.json"),
        config={"SIM_DEFAULT_TICK_SIZE": TICK_SIZE, "SIM_DEFAULT_TICK_VALUE": TICK_VALUE},
        sim_account_settings_by_id=_make_sim_account_settings(),
        price_feed=price_feed,
    )

    open_orders = restarted.handle_post("/api/Order/searchOpen", {"accountId": SIM001_ID})["orders"]
    positions = restarted.handle_post("/api/Position/searchOpen", {"accountId": SIM001_ID})["positions"]
    assert len(open_orders) == 2
    assert len(positions) == 1
