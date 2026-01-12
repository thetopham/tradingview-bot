import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from simbroker import InMemoryBarLoader, SimBroker, ORDER_STATUS_CANCELLED, ORDER_STATUS_FILLED


def _base_config(state_path):
    return {
        "ACCOUNTS": {"sim": 1},
        "SUPABASE_URL": None,
        "SUPABASE_KEY": None,
        "OVERRIDE_CONTRACT_ID": "CON.F.US.MES.H26",
        "SIMBROKER_STATE_PATH": str(state_path),
        "SIM_ACCOUNT_BALANCE": 50000.0,
        "SIM_BRACKET_SL_USD": 5.0,
        "SIM_BRACKET_TP_USD": 5.0,
        "SIM_TICK_SIZE": 1.0,
        "SIM_TICK_VALUE": 1.0,
        "SIM_FILL_POLICY": "worst",
    }


def _bar(ts, o, h, l, c, v=0):
    return {
        "t": ts,
        "o": o,
        "h": h,
        "l": l,
        "c": c,
        "v": v,
    }


def test_place_market_returns_projectx_shape(tmp_path):
    bars = [_bar("2024-01-01T00:00:00+00:00", 100, 101, 99, 100)]
    broker = SimBroker(_base_config(tmp_path / "state.json"), bar_loader=InMemoryBarLoader(bars))

    response = broker.dispatch(
        "/api/Order/place",
        {"accountId": 1, "contractId": "CON.F.US.MES.H26", "type": 2, "side": 0, "size": 1},
    )

    assert response["success"] is True
    assert response["errorCode"] == 0
    assert response["errorMessage"] is None
    assert isinstance(response["orderId"], int)


def test_search_open_returns_synthetic_brackets(tmp_path):
    bars = [_bar("2024-01-01T00:00:00+00:00", 100, 101, 99, 100)]
    broker = SimBroker(_base_config(tmp_path / "state.json"), bar_loader=InMemoryBarLoader(bars))

    broker.dispatch(
        "/api/Order/place",
        {"accountId": 1, "contractId": "CON.F.US.MES.H26", "type": 2, "side": 0, "size": 1},
    )
    open_orders = broker.dispatch("/api/Order/searchOpen", {"accountId": 1})["orders"]

    assert len(open_orders) == 2
    assert {order["type"] for order in open_orders} == {1, 4}


def test_tp_hit_closes_position_and_records_trade(tmp_path):
    bar_loader = InMemoryBarLoader([
        _bar("2024-01-01T00:00:00+00:00", 100, 101, 99, 100),
    ])
    broker = SimBroker(_base_config(tmp_path / "state.json"), bar_loader=bar_loader)

    broker.dispatch(
        "/api/Order/place",
        {"accountId": 1, "contractId": "CON.F.US.MES.H26", "type": 2, "side": 0, "size": 1},
    )
    bar_loader._bars.append(_bar("2024-01-01T00:01:00+00:00", 100, 106, 99, 105))
    broker.sim_update(1, "CON.F.US.MES.H26", now_ts_iso="2024-01-01T00:01:30+00:00")

    positions = broker.dispatch("/api/Position/searchOpen", {"accountId": 1})["positions"]
    assert positions == []

    orders = broker._state["orders"]
    stop_order = next(order for order in orders if order.get("customTag") == "sim_bracket_sl")
    limit_order = next(order for order in orders if order.get("customTag") == "sim_bracket_tp")

    assert limit_order["status"] == ORDER_STATUS_FILLED
    assert stop_order["status"] == ORDER_STATUS_CANCELLED

    trades = broker.dispatch(
        "/api/Trade/search",
        {"accountId": 1, "startTimestamp": "2024-01-01T00:00:00+00:00"},
    )["trades"]

    assert len(trades) == 2
    exit_trade = next(trade for trade in trades if trade["profitAndLoss"] is not None)
    assert exit_trade["profitAndLoss"] > 0


def test_state_persists_across_reloads(tmp_path):
    state_path = tmp_path / "state.json"
    bars = [_bar("2024-01-01T00:00:00+00:00", 100, 101, 99, 100)]
    broker = SimBroker(_base_config(state_path), bar_loader=InMemoryBarLoader(bars))

    response = broker.dispatch(
        "/api/Order/place",
        {"accountId": 1, "contractId": "CON.F.US.MES.H26", "type": 2, "side": 0, "size": 1},
    )
    first_order_id = response["orderId"]

    broker_reloaded = SimBroker(_base_config(state_path), bar_loader=InMemoryBarLoader(bars))
    reloaded_orders = broker_reloaded.dispatch("/api/Order/search", {"accountId": 1})["orders"]

    assert any(order["id"] == first_order_id for order in reloaded_orders)
