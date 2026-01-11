import importlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("t,o,h,l,c,v\n")
        for row in rows:
            handle.write(
                f"{row['t']},{row['o']},{row['h']},{row['l']},{row['c']},{row['v']}\n"
            )


def _reload_modules():
    for module_name in ["config", "simbroker", "api", "auth"]:
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
        else:
            importlib.import_module(module_name)


def test_sim_market_order_brackets_and_persistence(monkeypatch):
    monkeypatch.setenv("BROKER_MODE", "sim")
    monkeypatch.setenv("ACCOUNT_TEST", "1")
    monkeypatch.setenv("PROJECTX_BASE_URL", "http://example.com")
    monkeypatch.setenv("SIM_BRACKET_SL_USD", "5")
    monkeypatch.setenv("SIM_BRACKET_TP_USD", "5")
    monkeypatch.setenv("SIM_TICK_SIZE", "1")
    monkeypatch.setenv("SIM_TICK_VALUE", "1")
    monkeypatch.setenv("SIM_FILL_POLICY", "worst")
    monkeypatch.setenv("OVERRIDE_CONTRACT_ID", "CON.F.US.MES.H26")

    data_dir = Path("/mnt/data")
    state_path = data_dir / "simbroker_state.json"
    if state_path.exists():
        state_path.unlink()

    bars = []
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for i, close_price in enumerate([100, 101, 102, 110]):
        ts = start + timedelta(minutes=5 * i)
        bars.append(
            {
                "t": ts.isoformat(),
                "o": close_price - 1,
                "h": close_price + (10 if i == 3 else 1),
                "l": close_price - 1,
                "c": close_price,
                "v": 10,
            }
        )
    _write_csv(data_dir / "tv_datafeed_5m_rows (2).csv", bars)

    _reload_modules()
    import api
    import simbroker

    acct_id = 1
    contract_id = "CON.F.US.MES.H26"
    response = api.place_market(acct_id, contract_id, 0, 1)
    assert response.get("success") is True
    assert isinstance(response.get("orderId"), int)
    assert response.get("errorCode") == 0
    assert response.get("errorMessage") is None

    open_orders = api.search_open(acct_id)
    assert len(open_orders) == 2
    assert {o.get("type") for o in open_orders} == {1, 4}

    next_bar = {
        "t": (start + timedelta(minutes=5 * len(bars))).isoformat(),
        "o": 111,
        "h": 121,
        "l": 109,
        "c": 120,
        "v": 10,
    }
    bars.append(next_bar)
    _write_csv(data_dir / "tv_datafeed_5m_rows (2).csv", bars)

    simbroker.sim_update(acct_id, contract_id, now_ts_iso=next_bar["t"])

    positions = api.search_pos(acct_id)
    assert positions == []

    open_orders_after = api.search_open(acct_id)
    assert open_orders_after == []

    since = datetime.now(timezone.utc) - timedelta(days=1)
    trades = api.search_trades(acct_id, since)
    assert any(t.get("profitAndLoss") is not None for t in trades)

    new_broker = simbroker.SimBroker()
    assert len(new_broker.state.get("orders", [])) >= 1
    assert len(new_broker.state.get("trades", [])) >= 1
