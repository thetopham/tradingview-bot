import importlib
import os
from datetime import datetime, timezone, timedelta

import pytest


STATE_PATH = "/mnt/data/simbroker_state.json"
FEED_PATH = "/mnt/data/tv_datafeed_5m_rows (2).csv"


def _write_feed(rows):
    os.makedirs(os.path.dirname(FEED_PATH), exist_ok=True)
    with open(FEED_PATH, "w", encoding="utf-8") as handle:
        handle.write("t,o,h,l,c,v\n")
        for row in rows:
            handle.write(
                f"{row['t']},{row['o']},{row['h']},{row['l']},{row['c']},{row['v']}\n"
            )


def _reset_state():
    if os.path.exists(STATE_PATH):
        os.unlink(STATE_PATH)


@pytest.fixture(autouse=True)
def _sim_env(monkeypatch):
    monkeypatch.setenv("ACCOUNT_TEST", "1001")
    monkeypatch.setenv("PROJECTX_BASE_URL", "http://example.test")
    monkeypatch.setenv("PROJECTX_USERNAME", "user")
    monkeypatch.setenv("PROJECTX_API_KEY", "key")
    monkeypatch.setenv("BROKER_MODE", "sim")
    monkeypatch.setenv("SIM_BRACKET_SL_USD", "30")
    monkeypatch.setenv("SIM_BRACKET_TP_USD", "60")
    monkeypatch.setenv("SIM_TICK_SIZE", "0.25")
    monkeypatch.setenv("SIM_TICK_VALUE", "1.25")
    monkeypatch.setenv("SIM_FILL_POLICY", "worst")
    _reset_state()
    yield
    _reset_state()


def _reload_modules():
    import config
    import simbroker
    import api

    importlib.reload(config)
    importlib.reload(simbroker)
    importlib.reload(api)
    return config, simbroker, api


def test_place_market_response():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    _write_feed([
        {"t": now.isoformat(), "o": 100, "h": 101, "l": 99, "c": 100, "v": 10}
    ])
    _, simbroker, api = _reload_modules()

    response = api.place_market(1001, "CON.F.US.MES.H26", 0, 1)

    assert response["success"] is True
    assert isinstance(response["orderId"], int)
    assert response["errorCode"] == 0


def test_bracket_orders_are_open():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    _write_feed([
        {"t": now.isoformat(), "o": 100, "h": 101, "l": 99, "c": 100, "v": 10}
    ])
    _, simbroker, api = _reload_modules()

    api.place_market(1001, "CON.F.US.MES.H26", 0, 1)
    open_orders = api.search_open(1001)

    stop_orders = [o for o in open_orders if o.get("type") == 4]
    limit_orders = [o for o in open_orders if o.get("type") == 1]

    assert len(stop_orders) == 1
    assert len(limit_orders) == 1


def test_sim_update_hits_tp_and_closes():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    entry_bar = {"t": now.isoformat(), "o": 100, "h": 101, "l": 99, "c": 100, "v": 10}
    _write_feed([entry_bar])
    _, simbroker, api = _reload_modules()

    broker = simbroker.get_broker()
    api.place_market(1001, "CON.F.US.MES.H26", 0, 1)

    next_bar_time = now + timedelta(minutes=5)
    broker._feed._bars.append(
        {"t": next_bar_time, "o": 100, "h": 115, "l": 99, "c": 114, "v": 12}
    )

    broker.sim_update(1001, "CON.F.US.MES.H26", now_ts_iso=next_bar_time.isoformat())

    assert api.search_pos(1001) == []
    trades = api.search_trades(1001, datetime(2023, 12, 31, tzinfo=timezone.utc))
    assert len(trades) >= 2
    exit_trades = [t for t in trades if t.get("profitAndLoss") is not None]
    assert exit_trades

    open_orders = api.search_open(1001)
    assert open_orders == []


def test_state_persists_across_restart():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    _write_feed([
        {"t": now.isoformat(), "o": 100, "h": 101, "l": 99, "c": 100, "v": 10}
    ])
    _, simbroker, api = _reload_modules()

    response = api.place_market(1001, "CON.F.US.MES.H26", 0, 1)
    order_id = response["orderId"]

    importlib.reload(simbroker)
    broker = simbroker.SimBroker()

    orders = [o for o in broker._state.get("orders", []) if o.get("id") == order_id]
    assert orders
