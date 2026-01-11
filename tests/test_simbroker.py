import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

def _write_csv(path):
    content = """t,o,h,l,c,v
2024-01-01T00:00:00+00:00,100,101,99,100,10
2024-01-01T00:05:00+00:00,100,106,94,100,12
"""
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def sim_env(tmp_path, monkeypatch):
    data_path = tmp_path / "bars.csv"
    _write_csv(data_path)

    state_path = tmp_path / "state.json"

    monkeypatch.setenv("ACCOUNT_TEST", "1")
    monkeypatch.setenv("BROKER_MODE", "sim")
    monkeypatch.setenv("SIM_STATE_PATH", str(state_path))
    monkeypatch.setenv("SIM_DATA_PATHS", str(data_path))
    monkeypatch.setenv("SIM_BRACKET_SL_USD", "5")
    monkeypatch.setenv("SIM_BRACKET_TP_USD", "5")
    monkeypatch.setenv("SIM_TICK_SIZE", "1")
    monkeypatch.setenv("SIM_TICK_VALUE", "1")
    monkeypatch.setenv("SIM_FILL_POLICY", "best")
    monkeypatch.setenv("SIM_START_BALANCE", "50000")

    return {
        "data_path": data_path,
        "state_path": state_path,
    }


def test_place_market_and_brackets(sim_env):
    from simbroker import SimBroker

    broker = SimBroker(state_path=str(sim_env["state_path"]), data_paths=[str(sim_env["data_path"])])
    payload = {
        "accountId": 1,
        "contractId": "CON.F.US.MES.H26",
        "type": 2,
        "side": 0,
        "size": 1,
    }
    response = broker.handle_request("/api/Order/place", payload)

    assert response["success"] is True
    assert response["errorCode"] == 0
    assert isinstance(response["orderId"], int)

    open_orders = broker.handle_request("/api/Order/searchOpen", {"accountId": 1})["orders"]
    assert len(open_orders) == 2
    order_types = sorted(order["type"] for order in open_orders)
    assert order_types == [1, 4]


def test_sim_update_closes_position(sim_env):
    from simbroker import SimBroker

    broker = SimBroker(state_path=str(sim_env["state_path"]), data_paths=[str(sim_env["data_path"])])
    payload = {
        "accountId": 1,
        "contractId": "CON.F.US.MES.H26",
        "type": 2,
        "side": 0,
        "size": 1,
    }
    broker.handle_request("/api/Order/place", payload)

    broker.sim_update(1, "CON.F.US.MES.H26", "2024-01-01T00:05:00+00:00")

    positions = broker.handle_request("/api/Position/searchOpen", {"accountId": 1})["positions"]
    assert positions == []

    open_orders = broker.handle_request("/api/Order/searchOpen", {"accountId": 1})["orders"]
    assert open_orders == []

    trades = broker.handle_request(
        "/api/Trade/search",
        {
            "accountId": 1,
            "startTimestamp": "1970-01-01T00:00:00+00:00",
            "endTimestamp": "2100-01-01T00:00:00+00:00",
        },
    )["trades"]
    assert len(trades) >= 2
    assert any(trade.get("profitAndLoss") != 0 for trade in trades)


def test_state_persists_across_restarts(sim_env):
    from simbroker import SimBroker

    broker = SimBroker(state_path=str(sim_env["state_path"]), data_paths=[str(sim_env["data_path"])])
    broker.handle_request(
        "/api/Order/place",
        {
            "accountId": 1,
            "contractId": "CON.F.US.MES.H26",
            "type": 2,
            "side": 0,
            "size": 1,
        },
    )

    broker2 = SimBroker(state_path=str(sim_env["state_path"]), data_paths=[str(sim_env["data_path"])])
    assert len(broker2.state["orders"]) >= 1
    assert broker2.state["nextOrderId"] > 1

    with open(sim_env["state_path"], "r", encoding="utf-8") as handle:
        persisted = json.load(handle)
    assert persisted["orders"]
