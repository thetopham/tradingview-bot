"""Numeric-only operation must not resurrect retired simulator experiments."""
from datetime import datetime, timezone
import importlib
import sys

import pytest

from tvbot_v2.simulate.ledger import SimLedger
from tvbot_v2.simulate.portfolio import Bracket, SimVariant


@pytest.fixture
def numeric_only_sim(tmp_path, monkeypatch):
    db = tmp_path / "sim.sqlite"
    SimLedger(db).register(tuple(
        SimVariant(name, "30m", name + "_fixture", {1: Bracket(1, 24, 48)}, "1m")
        for name in ("epsilon", "epsilon_vision", "zeta")
    ))
    monkeypatch.setenv("BROKER_MODE", "sim")
    monkeypatch.setenv("SIM_BROKER_DB", str(db))
    monkeypatch.setenv("SIM_ACTIVE_ACCOUNTS", "epsilon")
    monkeypatch.setenv("SIM_DECISION_SOURCE", "scheduler")
    monkeypatch.setenv("WEBHOOK_SECRET", "test-only")
    for key in ("PROJECTX_BASE_URL", "PROJECTX_USERNAME", "PROJECTX_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    import logging_config
    monkeypatch.setattr(logging_config, "setup_logging", lambda: None)
    modules = ("tradingview_projectx_bot", "dashboard", "sim_dashboard", "position_manager",
               "api", "auth", "config")
    for name in modules:
        sys.modules.pop(name, None)
    bot = importlib.import_module("tradingview_projectx_bot")
    yield bot
    for name in modules:
        sys.modules.pop(name, None)


def test_scheduler_only_calls_enabled_numeric_accounts(numeric_only_sim, monkeypatch):
    bot = numeric_only_sim
    # Even a stale route added after startup cannot revive the retired cohort.
    for account in ("epsilon", "epsilon_vision", "zeta"):
        bot.AI_TEST_ENDPOINTS[account] = "https://unused.invalid/overseer"
    calls = []
    monkeypatch.setattr(bot, "ai_trade_decision", lambda account, *a, **k:
                        calls.append(account) or {"signal": "HOLD", "size": 1})
    from brokers.sim_decision_feed import record
    from scripts.run_sim_decisions import run_pending
    record(bot.config["SIM_BROKER_DB"], "30m", {
        "timestamp": "2026-09-22T14:00:00+00:00", "open": 6000, "high": 6001,
        "low": 5999, "close": 6000, "volume": 100,
    })
    now = datetime(2026, 9, 22, 14, 31, tzinfo=timezone.utc)
    assert run_pending(bot, now) == [("epsilon", "HOLD")]
    assert calls == ["epsilon"]


def test_scheduler_does_not_call_manually_paused_numeric_account(numeric_only_sim, monkeypatch):
    bot = numeric_only_sim
    bot.get_sim_adapter().ledger.set_pause("epsilon", True)
    bot.AI_TEST_ENDPOINTS["epsilon"] = "https://unused.invalid/overseer"
    monkeypatch.setattr(bot, "ai_trade_decision", lambda *a, **k:
                        (_ for _ in ()).throw(AssertionError("paused model called")))
    from brokers.sim_decision_feed import record
    from scripts.run_sim_decisions import run_pending
    record(bot.config["SIM_BROKER_DB"], "30m", {
        "timestamp": "2026-09-22T14:00:00+00:00", "open": 6000, "high": 6001,
        "low": 5999, "close": 6000, "volume": 100,
    })
    assert run_pending(bot, datetime(2026, 9, 22, 14, 31, tzinfo=timezone.utc)) == []


@pytest.mark.parametrize("account", ["epsilon_vision", "zeta"])
@pytest.mark.parametrize("with_bar", [True, False])
def test_retired_or_unselected_decisions_fail_closed(numeric_only_sim, account, with_bar):
    payload = {"secret": "test-only", "account": account,
               "decision": {"signal": "BUY", "size": 1}}
    if with_bar:
        payload["bar"] = {"timestamp": "2026-09-22T14:00:00Z", "open": 6000,
                          "high": 6001, "low": 5999, "close": 6000, "volume": 100}
    response = numeric_only_sim.app.test_client().post("/webhook", json=payload)
    assert response.status_code == 422
    assert "disabled" in response.json["error"]
    with numeric_only_sim.get_sim_adapter().ledger.connection() as conn:
        assert conn.execute("SELECT count(*) FROM sim_decision").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sim_order").fetchone()[0] == 0


def test_retired_account_brackets_still_receive_execution_bars(numeric_only_sim, monkeypatch):
    bot = numeric_only_sim
    adapter = bot.get_sim_adapter()
    adapter.ledger.set_pause("epsilon_vision", True)
    with adapter.ledger.connection() as conn:
        conn.execute("INSERT INTO sim_position(account,generation,direction,quantity,entry_price,"
                     "entry_ts,stop_price,target_price) VALUES (?,?,?,?,?,?,?,?)",
                     ("epsilon_vision", 1, 1, 1, 6000, "2026-09-22T14:28:00+00:00", 5994, 6012))
    monkeypatch.setattr(bot, "ai_trade_decision", lambda *a, **k:
                        (_ for _ in ()).throw(AssertionError("execution feed called model")))
    response = bot.app.test_client().post("/sim/feed?source_table=tv_datafeed",
        json={"symbol": "MES", "timeframe": "1", "o": 6000, "h": 6001,
              "l": 5993, "c": 5994, "v": 100, "ts": "2026-09-22T14:30:02Z"},
        headers={"X-Webhook-Secret": "test-only"})
    assert response.status_code == 200
    assert response.json["accounts"]["epsilon_vision"]["position"] is None
    assert response.json["accounts"]["epsilon_vision"]["trade_count"] == 1
    assert response.json["accounts"]["epsilon_vision"]["manual_paused"] is True


@pytest.mark.parametrize("selection", ["", ",", "unknown", "EPSILON", "epsilon,epsilon", "epsilon_vision"])
def test_invalid_active_account_selection_fails_closed(numeric_only_sim, monkeypatch, selection):
    monkeypatch.setenv("SIM_ACTIVE_ACCOUNTS", selection)
    from config import load_config
    with pytest.raises((ValueError, RuntimeError), match="SIM_ACTIVE_ACCOUNTS"):
        load_config()


def test_decision_feed_targets_only_numeric_cohort(numeric_only_sim):
    response = numeric_only_sim.app.test_client().post("/sim/feed?source_table=tv_datafeed_30m",
        json={"symbol": "MES", "timeframe": "30", "o": 6000, "h": 6001,
              "l": 5999, "c": 6000, "v": 100, "ts": "2026-09-22T14:30:02Z"},
        headers={"X-Webhook-Secret": "test-only"})
    assert response.status_code == 200
    assert response.json["accounts"] == ["epsilon"]
