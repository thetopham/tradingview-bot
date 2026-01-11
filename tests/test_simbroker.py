import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from simbroker import SimBroker


CONTRACT_ID = "CON.F.US.MES.H26"
ACCOUNT_ID = 111


def _write_bars(path: Path, bars: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["t", "o", "h", "l", "c", "v"])
        writer.writeheader()
        for bar in bars:
            writer.writerow(bar)


def _append_bar(path: Path, bar: dict) -> None:
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["t", "o", "h", "l", "c", "v"])
        writer.writerow(bar)


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture()
def sim_env(tmp_path, monkeypatch):
    bars_path = tmp_path / "bars.csv"
    state_path = tmp_path / "state.json"

    monkeypatch.setenv("ACCOUNT_TEST", str(ACCOUNT_ID))
    monkeypatch.setenv("SIMBROKER_STATE_PATH", str(state_path))
    monkeypatch.setenv("SIM_DATAFEED_PATHS", str(bars_path))
    monkeypatch.setenv("SIM_TICK_SIZE", "1")
    monkeypatch.setenv("SIM_TICK_VALUE", "1")
    monkeypatch.setenv("SIM_BRACKET_SL_USD", "5")
    monkeypatch.setenv("SIM_BRACKET_TP_USD", "10")
    monkeypatch.setenv("SIM_FILL_POLICY", "worst")
    monkeypatch.setenv("BROKER_MODE", "sim")

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = [
        {"t": _epoch_ms(start), "o": 100, "h": 101, "l": 99, "c": 100, "v": 10},
        {"t": _epoch_ms(start.replace(minute=5)), "o": 100, "h": 101, "l": 99, "c": 100, "v": 12},
    ]
    _write_bars(bars_path, bars)

    return {"bars_path": bars_path, "state_path": state_path}


def test_place_market_returns_projectx_shape(sim_env):
    broker = SimBroker(state_path=str(sim_env["state_path"]))
    resp = broker.handle_request(
        "/api/Order/place",
        {
            "accountId": ACCOUNT_ID,
            "contractId": CONTRACT_ID,
            "type": 2,
            "side": 0,
            "size": 1,
        },
    )

    assert resp["success"] is True
    assert isinstance(resp["orderId"], int)
    assert resp["errorCode"] == 0


def test_search_open_returns_synthetic_brackets(sim_env):
    broker = SimBroker(state_path=str(sim_env["state_path"]))
    broker.handle_request(
        "/api/Order/place",
        {
            "accountId": ACCOUNT_ID,
            "contractId": CONTRACT_ID,
            "type": 2,
            "side": 0,
            "size": 1,
        },
    )

    resp = broker.handle_request("/api/Order/searchOpen", {"accountId": ACCOUNT_ID})
    orders = resp["orders"]

    assert len(orders) == 2
    types = {order["type"] for order in orders}
    assert types == {1, 4}
    assert all(order["status"] == 1 for order in orders)


def test_tp_bar_closes_position_and_records_trades(sim_env):
    broker = SimBroker(state_path=str(sim_env["state_path"]))
    broker.handle_request(
        "/api/Order/place",
        {
            "accountId": ACCOUNT_ID,
            "contractId": CONTRACT_ID,
            "type": 2,
            "side": 0,
            "size": 1,
        },
    )

    next_bar = {
        "t": _epoch_ms(datetime(2024, 1, 1, 0, 10, tzinfo=timezone.utc)),
        "o": 100,
        "h": 112,
        "l": 98,
        "c": 110,
        "v": 15,
    }
    _append_bar(sim_env["bars_path"], next_bar)

    broker.sim_update(ACCOUNT_ID, CONTRACT_ID)

    orders = broker.handle_request("/api/Order/searchOpen", {"accountId": ACCOUNT_ID})["orders"]
    positions = broker.handle_request("/api/Position/searchOpen", {"accountId": ACCOUNT_ID})["positions"]
    trades = broker.handle_request("/api/Trade/search", {"accountId": ACCOUNT_ID})["trades"]

    assert orders == []
    assert positions == []
    assert len(trades) == 2
    assert any(trade.get("profitAndLoss") is not None for trade in trades)


def test_state_persists_across_instances(sim_env):
    broker = SimBroker(state_path=str(sim_env["state_path"]))
    broker.handle_request(
        "/api/Order/place",
        {
            "accountId": ACCOUNT_ID,
            "contractId": CONTRACT_ID,
            "type": 2,
            "side": 0,
            "size": 1,
        },
    )

    reload_broker = SimBroker(state_path=str(sim_env["state_path"]))
    orders = reload_broker.handle_request("/api/Order/searchOpen", {"accountId": ACCOUNT_ID})["orders"]

    assert len(orders) == 2
