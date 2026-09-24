"""The legacy webhook can use v2 without ProjectX authentication or SignalR."""

from datetime import datetime, timezone
import importlib
import sys

import pytest

from tvbot_v2.simulate.ledger import SimLedger
from tvbot_v2.simulate.portfolio import Bracket, SimVariant


def _bar(timestamp, *, low=5999, close=6000):
    return {"timestamp": timestamp, "open": 6000, "high": 6001,
            "low": low, "close": close, "volume": 100}


@pytest.fixture
def legacy_sim(tmp_path, monkeypatch):
    db = tmp_path / "sim.sqlite"
    SimLedger(db).register((SimVariant("epsilon", "30m", "prodex_test",
                                       {1: Bracket(1, 24, 48)}),
                            SimVariant("zeta", "30m", "variant_test",
                                       {1: Bracket(1, 24, 48)})))
    monkeypatch.setenv("BROKER_MODE", "sim")
    monkeypatch.setenv("SIM_BROKER_DB", str(db))
    monkeypatch.setenv("WEBHOOK_SECRET", "test-only")
    monkeypatch.delenv("PROJECTX_BASE_URL", raising=False)
    monkeypatch.delenv("PROJECTX_USERNAME", raising=False)
    monkeypatch.delenv("PROJECTX_API_KEY", raising=False)
    import logging_config
    monkeypatch.setattr(logging_config, "setup_logging", lambda: None)
    for name in ("tradingview_projectx_bot", "dashboard", "position_manager", "api", "auth", "config"):
        sys.modules.pop(name, None)
    bot = importlib.import_module("tradingview_projectx_bot")
    yield bot
    for name in ("tradingview_projectx_bot", "dashboard", "position_manager", "api", "auth", "config"):
        sys.modules.pop(name, None)


@pytest.fixture
def one_minute_sim(tmp_path, monkeypatch):
    db = tmp_path / "sim.sqlite"
    SimLedger(db).register((SimVariant("epsilon", "30m", "prodex_test",
                                       {1: Bracket(1, 24, 48)}, "1m"),))
    monkeypatch.setenv("BROKER_MODE", "sim")
    monkeypatch.setenv("SIM_BROKER_DB", str(db))
    monkeypatch.setenv("WEBHOOK_SECRET", "test-only")
    monkeypatch.delenv("PROJECTX_BASE_URL", raising=False)
    monkeypatch.delenv("PROJECTX_USERNAME", raising=False)
    monkeypatch.delenv("PROJECTX_API_KEY", raising=False)
    import logging_config
    monkeypatch.setattr(logging_config, "setup_logging", lambda: None)
    for name in ("tradingview_projectx_bot", "dashboard", "position_manager", "api", "auth", "config"):
        sys.modules.pop(name, None)
    bot = importlib.import_module("tradingview_projectx_bot")
    yield bot
    for name in ("tradingview_projectx_bot", "dashboard", "position_manager", "api", "auth", "config"):
        sys.modules.pop(name, None)


def test_one_minute_feed_advances_execution_without_calling_overseer(one_minute_sim, monkeypatch):
    bot = one_minute_sim
    monkeypatch.setattr(bot, "ai_trade_decision", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("overseer called on 1m feed")))
    client = bot.app.test_client()
    base = {"symbol": "MES", "timeframe": "1", "o": 6000, "h": 6001,
            "l": 5999, "c": 6000, "v": 100}
    first = client.post("/sim/feed?source_table=tv_datafeed", json={**base,
                        "ts": "2026-09-22T14:30:02Z"},
                        headers={"X-Webhook-Secret": "test-only"})
    assert first.status_code == 200
    assert first.json["bar_ts"] == "2026-09-22T14:29:00+00:00"
    assert first.json["accounts"]["epsilon"]["position"] is None
    decision = client.post("/webhook", json={"secret": "test-only", "account": "epsilon",
                           "bar": _bar("2026-09-22T14:00:00Z"),
                           "decision": {"signal": "BUY", "size": 1}})
    assert decision.status_code == 200
    assert decision.json["account"]["pending"]["signal"] == "BUY"
    with bot.get_sim_adapter().ledger.connection() as conn:
        assert conn.execute("SELECT count(*) FROM sim_decision").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM sim_bar").fetchone()[0] == 1


def test_scheduler_caches_decision_bar_and_calls_overseer_once(one_minute_sim, monkeypatch):
    bot = one_minute_sim
    bot.config["SIM_DECISION_SOURCE"] = "scheduler"
    bot.AI_TEST_ENDPOINTS["epsilon"] = "http://unused.invalid/overseer"
    calls = []

    def decide(*args, **kwargs):
        calls.append(args[0])
        return {"signal": "BUY", "size": 1, "ai_decision_id": 1234}

    monkeypatch.setattr(bot, "ai_trade_decision", decide)
    row = {"symbol": "MES", "timeframe": "30", "o": 6000, "h": 6001,
           "l": 5999, "c": 6000, "v": 100, "ts": "2026-09-22T14:30:02Z"}
    client = bot.app.test_client()
    headers = {"X-Webhook-Secret": "test-only"}
    response = client.post("/sim/feed?source_table=tv_datafeed_30m",
                           json=row, headers=headers)
    assert response.status_code == 200
    assert response.json["status"] == "cached"
    assert calls == []
    from scripts.run_sim_decisions import run_pending
    now = datetime(2026, 9, 22, 14, 31, tzinfo=timezone.utc)
    assert run_pending(bot, now) == [("epsilon", "BUY")]
    assert run_pending(bot, now) == []
    assert calls == ["epsilon"]
    with bot.get_sim_adapter().ledger.connection() as conn:
        decision = conn.execute("SELECT decision_json FROM sim_decision").fetchone()[0]
    assert '"decision_id":"1234"' in decision


def test_scheduler_webhook_queues_broker_order_for_one_minute_fill(one_minute_sim, monkeypatch):
    bot = one_minute_sim
    adapter = bot.get_sim_adapter()
    original = adapter.broker.submit_order
    available = datetime.fromisoformat("2026-09-22T14:30:20+00:00")
    monkeypatch.setattr(adapter.broker, "submit_order",
                        lambda *args, **kwargs: original(*args, available_at=available, **kwargs))
    client = bot.app.test_client()
    headers = {"X-Webhook-Secret": "test-only"}
    row = {"symbol": "MES", "timeframe": "1", "o": 6000, "h": 6001,
           "l": 5999, "c": 6000, "v": 100}
    assert client.post("/sim/feed?source_table=tv_datafeed",
                       json={**row, "ts": "2026-09-22T14:30:02Z"},
                       headers=headers).status_code == 200
    payload = {"secret": "test-only", "account": "epsilon",
               "decision": {"signal": "BUY", "size": 1, "ai_decision_id": 123},
               "client_order_id": "decision-123"}
    queued = client.post("/webhook", json=payload)
    assert queued.status_code == 200
    assert queued.json["account"]["order"]["status"] == "queued"
    assert client.post("/webhook", json=payload).json == queued.json
    before = client.post("/sim/feed?source_table=tv_datafeed",
                         json={**row, "ts": "2026-09-22T14:31:02Z"},
                         headers=headers)
    assert before.json["accounts"]["epsilon"]["position"] is None
    filled = client.post("/sim/feed?source_table=tv_datafeed",
                         json={**row, "ts": "2026-09-22T14:32:02Z"},
                         headers=headers)
    assert filled.json["accounts"]["epsilon"]["position"]["entry_price"] == 6000.25
    stopped = client.post("/sim/feed?source_table=tv_datafeed",
                          json={**row, "l": 5993, "ts": "2026-09-22T14:33:02Z"},
                          headers=headers)
    assert stopped.json["accounts"]["epsilon"]["trade_count"] == 1
    result = client.get("/sim/results?after_id=0", headers=headers).json["results"][0]
    assert result["payload"]["ai_decision_id"] == 123
    assert result["payload"]["order_id"] == '["' + queued.json["account"]["order"]["orderId"] + '"]'
    broker_events = client.get("/sim/broker-events?after_id=0", headers=headers)
    assert broker_events.status_code == 200
    names = [event["event"] for event in broker_events.json["events"]]
    assert "GatewayUserOrder" in names
    assert "GatewayUserPosition" in names
    assert "GatewayUserTrade" in names
    cursor = broker_events.json["next_cursor"]
    assert client.get(f"/sim/broker-events?after_id={cursor}", headers=headers).json["events"] == []


def test_projectx_broker_helpers_use_local_order_queue(one_minute_sim):
    api = sys.modules["api"]
    account_id = one_minute_sim.ACCOUNTS["epsilon"]
    contract = api.get_contract("MES")
    assert api.search_accounts()[0]["id"] == account_id
    assert api.search_pos(account_id) == []
    queued = api.place_market(account_id, contract, 0, 1,
                              client_order_id="api-order-1")
    assert queued["orderId"].startswith("SIM-")
    assert api.search_open(account_id)[0]["id"] == queued["orderId"]
    assert api.cancel(account_id, queued["orderId"]) == {"success": True}
    assert api.search_open(account_id) == []
    with pytest.raises(RuntimeError, match="disabled"):
        api.post("/api/Order/place", {"accountId": account_id})


def test_sim_webhook_persists_next_bar_fill_without_live_broker(legacy_sim, monkeypatch):
    bot = legacy_sim
    assert "signalr_listener" not in sys.modules
    client = bot.app.test_client()
    first = {"secret": "test-only", "account": "epsilon",
             "bar": _bar("2026-09-22T14:00:00Z"),
             "decision": {"signal": "BUY", "size": 1, "source": "fixture"}}
    assert client.post("/webhook", json={**first, "secret": "wrong"}).status_code == 403
    response = client.post("/webhook", json=first)
    assert response.status_code == 200
    assert response.json["account"]["pending"]["signal"] == "BUY"
    assert response.json["account"]["position"] is None
    assert client.post("/webhook", json=first).json == response.json
    second = {"secret": "test-only", "account": "epsilon",
              "bar": _bar("2026-09-22T14:30:00Z"),
              "decision": {"signal": "HOLD", "size": 1}}
    opened = client.post("/webhook", json=second)
    assert opened.status_code == 200
    assert opened.json["account"]["position"]["entry_price"] == 6000.25
    api = sys.modules["api"]
    account_id = bot.ACCOUNTS["epsilon"]
    assert account_id == 900001
    assert api.search_pos(account_id)[0]["contractId"] == "CON.F.US.MES.SIM"
    with pytest.raises(RuntimeError, match="disabled"):
        api.post("/api/Order/place", {"accountId": account_id})
    with pytest.raises(RuntimeError, match="disabled"):
        sys.modules["auth"].authenticate()
    assert "signalr_listener" not in sys.modules


def test_sim_stop_generates_legacy_trade_views(legacy_sim):
    client = legacy_sim.app.test_client()
    for ts, decision, low in (
        ("2026-09-22T14:00:00Z", {"signal": "BUY", "size": 1}, 5999),
        ("2026-09-22T14:30:00Z", {"signal": "HOLD", "size": 1}, 5999),
        ("2026-09-22T15:00:00Z", {"signal": "HOLD", "size": 1}, 5993),
    ):
        response = client.post("/webhook", json={"secret": "test-only", "account": "epsilon",
                                                "bar": _bar(ts, low=low, close=6000),
                                                "decision": decision})
        assert response.status_code == 200
    assert response.json["account"]["trade_count"] == 1
    api = sys.modules["api"]
    account_id = legacy_sim.ACCOUNTS["epsilon"]
    trades = api.search_trades(account_id, datetime(2026, 9, 22, tzinfo=timezone.utc))
    assert len(trades) == 2
    assert trades[0]["profitAndLoss"] is None
    assert trades[1]["profitAndLoss"] < 0
    assert trades[0]["contractId"] == "CON.F.US.MES.SIM"
    assert api.search_pos(account_id) == []

    # The close event is projected exactly once into a durable replacement
    # for the old SignalR-triggered trade_results write.
    assert client.get("/sim/results").status_code == 403
    result_response = client.get("/sim/results?after_id=0",
                                 headers={"X-Webhook-Secret": "test-only"})
    assert result_response.status_code == 200
    results = result_response.json["results"]
    assert len(results) == 1
    payload = results[0]["payload"]
    assert payload["account"] == "epsilon"
    assert payload["signal"] == "BUY"
    assert payload["size"] == 1
    assert payload["trace_id"].startswith("sim:epsilon:1:")
    assert payload["raw_trades"] == trades
    assert payload["net_pnl"] == pytest.approx(
        payload["total_pnl"] - payload["fees_total"])
    assert client.post("/webhook", json={"secret": "test-only", "account": "epsilon",
                                         "bar": _bar("2026-09-22T15:00:00Z", low=5993),
                                         "decision": {"signal": "HOLD", "size": 1}}).status_code == 200
    assert len(client.get("/sim/results?after_id=0",
                          headers={"X-Webhook-Secret": "test-only"}).json["results"]) == 1
    cursor = result_response.json["next_cursor"]
    assert client.get(f"/sim/results?after_id={cursor}",
                      headers={"X-Webhook-Secret": "test-only"}).json["results"] == []

    from scripts.publish_sim_results import publish_pending
    class Response:
        def __init__(self, body=None):
            self.body = body
        def raise_for_status(self):
            pass
        def json(self):
            return self.body
    class Session:
        def __init__(self):
            self.posts = []
        def get(self, *args, **kwargs):
            return Response([])
        def post(self, *args, **kwargs):
            self.posts.append(kwargs["json"])
            return Response()
    session = Session()
    adapter = api.get_sim_adapter()
    assert publish_pending(adapter, "https://example.invalid", "test-key", session=session) == 1
    assert session.posts == [payload]
    assert publish_pending(adapter, "https://example.invalid", "test-key", session=session) == 0
    assert len(session.posts) == 1


def test_sim_webhook_rejects_missing_bar(legacy_sim):
    response = legacy_sim.app.test_client().post("/webhook", json={
        "secret": "test-only", "account": "epsilon", "signal": "BUY"})
    assert response.status_code == 422
    assert "requires a decision or configured overseer" in response.json["error"]


def test_sim_events_replace_signalr_observation(legacy_sim):
    client = legacy_sim.app.test_client()
    assert client.get("/sim/events").status_code == 403
    first = client.get("/sim/events?after_id=0", headers={"X-Webhook-Secret": "test-only"})
    assert first.status_code == 200
    assert first.json["events"][0]["event_type"] == "account_created"
    cursor = first.json["next_cursor"]
    assert client.get(f"/sim/events?after_id={cursor}",
                      headers={"X-Webhook-Secret": "test-only"}).json == {
                          "events": [], "next_cursor": cursor}


def test_overseer_receives_sim_account_context_once(legacy_sim, monkeypatch):
    legacy_sim.AI_TEST_ENDPOINTS["epsilon"] = "http://unused.invalid/overseer"
    calls = []

    def decide(account, strategy, signal, symbol, size, alert, url,
               positions=None, position_context=None):
        calls.append((account, alert, positions, position_context))
        return {"signal": "BUY", "size": 1, "prompt_version": "test-prompt"}

    monkeypatch.setattr(legacy_sim, "ai_trade_decision", decide)
    envelope = {"secret": "test-only", "account": "epsilon",
                "bar": _bar("2026-09-22T14:00:00Z"), "signal": "HOLD"}
    client = legacy_sim.app.test_client()
    first = client.post("/webhook", json=envelope)
    second = client.post("/webhook", json=envelope)
    assert first.status_code == second.status_code == 200
    assert first.json == second.json
    assert len(calls) == 1
    assert calls[0][0] == "epsilon"
    assert calls[0][1].startswith("30m ")
    assert calls[0][3]["account_metrics"]["account_balance"] == 50000
    assert calls[0][3]["topstep"]["trailing_dd_remaining_usd"] == 2000


def test_forward_mode_rejects_stale_bar(legacy_sim):
    legacy_sim.config["SIM_MAX_BAR_LAG_SECONDS"] = 600
    response = legacy_sim.app.test_client().post("/webhook", json={
        "secret": "test-only", "account": "epsilon",
        "bar": _bar("2026-09-22T14:00:00Z"),
        "decision": {"signal": "BUY", "size": 1}})
    assert response.status_code == 422
    assert "too old" in response.json["error"]


def test_datafeed_fans_out_to_independent_same_timeframe_accounts(legacy_sim):
    client = legacy_sim.app.test_client()
    url = "/sim/feed?source_table=tv_datafeed_30m"
    headers = {"X-Webhook-Secret": "test-only"}
    first = {"ts": "2026-09-22T14:30:06Z", "timeframe": "30", "symbol": "MES",
             "o": 6000, "h": 6001, "l": 5999, "c": 6000, "v": 100,
             "decisions": {"epsilon": {"signal": "BUY", "size": 1},
                           "zeta": {"signal": "SELL", "size": 1}}}
    assert client.post(url, json=first).status_code == 403
    response = client.post(url, json=first, headers=headers)
    assert response.status_code == 200
    assert response.json["bar_ts"] == "2026-09-22T14:00:00+00:00"
    assert set(response.json["accounts"]) == {"epsilon", "zeta"}
    assert response.json["accounts"]["epsilon"]["pending"]["signal"] == "BUY"
    assert response.json["accounts"]["zeta"]["pending"]["signal"] == "SELL"
    assert client.post(url, json=first, headers=headers).json == response.json
    second = {**first, "ts": "2026-09-22T15:00:06Z",
              "decisions": {"epsilon": {"signal": "HOLD", "size": 1},
                            "zeta": {"signal": "HOLD", "size": 1}}}
    advanced = client.post(url, json=second, headers=headers)
    assert advanced.status_code == 200
    assert advanced.json["accounts"]["epsilon"]["position"]["direction"] == 1
    assert advanced.json["accounts"]["zeta"]["position"]["direction"] == -1


def test_sim_overseer_uses_broker_session_instead_of_legacy_wall_clock(legacy_sim, monkeypatch):
    api = sys.modules["api"]
    monkeypatch.setattr(api, "in_get_flat", lambda now: True)
    calls = []

    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"signal": "BUY", "size": 1}

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]["position_context"]))
        return Response()

    monkeypatch.setattr(api.session, "post", post)
    decision = api.ai_trade_decision("epsilon", "simple", "HOLD", "MES", 1,
                                     "30m", "http://overseer.invalid",
                                     position_context={"test": True})
    assert decision["signal"] == "BUY"
    assert calls == [("http://overseer.invalid", {"test": True})]
