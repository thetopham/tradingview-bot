import pytest
from datetime import datetime

from simbroker_assistant import (
    Bar,
    BarFeed,
    SimBroker,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_STOP,
    ORDER_SIDE_BID,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_FILLED,
    ORDER_STATUS_OPEN,
)


class StepBarFeed(BarFeed):
    """
    Deterministic, time-stepped feed.

    - Start with visible_bars=1 (only bar[0] visible)
    - Call advance() to reveal more bars
    """

    def __init__(self, streams, visible_bars: int = 1):
        self.streams = streams
        self.visible_bars = visible_bars

    def advance(self, n: int = 1):
        self.visible_bars += int(n)

    def _visible_stream(self, symbol: str, timeframe_filters):
        for tf in timeframe_filters:
            stream = self.streams.get((symbol, str(tf)))
            if stream:
                return stream[: self.visible_bars]
        return None

    def latest_bar(self, symbol: str, timeframe_filters):
        stream = self._visible_stream(symbol, timeframe_filters)
        if not stream:
            return None
        return stream[-1]

    def bars_between(self, symbol: str, timeframe_filters, start_ts_exclusive, end_ts_inclusive, limit=5000, order="asc"):
        def _parse(ts):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))

        stream = self._visible_stream(symbol, timeframe_filters)
        if not stream:
            return []

        start_dt = _parse(start_ts_exclusive) if start_ts_exclusive else None
        end_dt = _parse(end_ts_inclusive) if end_ts_inclusive else None

        out = []
        for b in stream:
            dt = _parse(b.ts)
            if start_dt and dt <= start_dt:
                continue
            if end_dt and dt > end_dt:
                continue
            out.append(b)

        out.sort(key=lambda b: _parse(b.ts))
        if order == "desc":
            out.reverse()
        return out[:limit]


@pytest.fixture
def env(monkeypatch):
    # deterministic sim account + bracket rule
    monkeypatch.setenv("ACCOUNT_SIM001", "900001")
    monkeypatch.setenv("SIM_SIM001_SL_USD", "10")
    monkeypatch.setenv("SIM_SIM001_TP_USD", "10")
    monkeypatch.setenv("SIM_FILL_POLICY", "worst")

    # Make USD->price conversion easy: 1 tick = $1, tickSize=1
    monkeypatch.setenv("SIM_TICK_SIZE", "1")
    monkeypatch.setenv("SIM_TICK_VALUE", "1")

    # Avoid accidental Supabase/CSV auto-detection in tests
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.delenv("SIM_BAR_FEED", raising=False)
    monkeypatch.delenv("SIM_CSV_FEED_PATH", raising=False)

    yield


def test_market_entry_creates_bracket_orders(tmp_path, env):
    # Bar1 is used for entry fill (c=100)
    bar1 = Bar(ts="2026-01-01T00:00:00+00:00", o=100, h=100, l=100, c=100, v=1)
    # Bar2 would later hit TP (high>=110)
    bar2 = Bar(ts="2026-01-01T00:05:00+00:00", o=100, h=111, l=99, c=110, v=1)

    feed = StepBarFeed({("MES", "1m"): [bar1, bar2]}, visible_bars=1)
    broker = SimBroker(state_path=str(tmp_path / "state.json"), bar_feed=feed)

    resp = broker.handle(
        "/api/Order/place",
        {
            "accountId": 900001,
            "contractId": "CON.F.US.MES.H26",
            "type": ORDER_TYPE_MARKET,
            "side": ORDER_SIDE_BID,
            "size": 1,
        },
    )
    assert resp["success"] is True
    assert "orderId" in resp

    open_orders = broker.handle("/api/Order/searchOpen", {"accountId": 900001})["orders"]
    assert len(open_orders) == 2, open_orders
    types = {o["type"] for o in open_orders}
    assert ORDER_TYPE_STOP in types
    assert ORDER_TYPE_LIMIT in types
    assert all(o["status"] == ORDER_STATUS_OPEN for o in open_orders)


def test_bracket_tp_closes_position_and_marks_orders(tmp_path, env):
    bar1 = Bar(ts="2026-01-01T00:00:00+00:00", o=100, h=100, l=100, c=100, v=1)
    bar2 = Bar(ts="2026-01-01T00:05:00+00:00", o=100, h=111, l=99, c=110, v=1)
    feed = StepBarFeed({("MES", "1m"): [bar1, bar2]}, visible_bars=1)
    broker = SimBroker(state_path=str(tmp_path / "state.json"), bar_feed=feed)

    broker.handle(
        "/api/Order/place",
        {
            "accountId": 900001,
            "contractId": "CON.F.US.MES.H26",
            "type": ORDER_TYPE_MARKET,
            "side": ORDER_SIDE_BID,
            "size": 1,
        },
    )

    # Reveal bar2 and advance sim; TP should trigger at 110
    feed.advance(1)
    events = broker.sim_update(900001, "CON.F.US.MES.H26")
    assert events, "expected a closure event"
    assert events[0]["reason"] == "tp"

    # Position is closed
    positions = broker.handle("/api/Position/searchOpen", {"accountId": 900001})["positions"]
    assert positions == []

    # Child orders: one filled, one cancelled
    orders = broker.handle(
        "/api/Order/search",
        {"accountId": 900001, "startTimestamp": "2025-12-31T00:00:00+00:00"},
    )["orders"]
    filled = [o for o in orders if o["status"] == ORDER_STATUS_FILLED and o["type"] in (ORDER_TYPE_STOP, ORDER_TYPE_LIMIT)]
    cancelled = [o for o in orders if o["status"] == ORDER_STATUS_CANCELLED and o["type"] in (ORDER_TYPE_STOP, ORDER_TYPE_LIMIT)]
    assert len(filled) == 1
    assert len(cancelled) == 1

    # Trades: entry + exit
    trades = broker.handle(
        "/api/Trade/search",
        {"accountId": 900001, "startTimestamp": "2025-12-31T00:00:00+00:00"},
    )["trades"]
    assert len(trades) >= 2

    # Exit trade has pnl = (110-100)*1 = 10
    exit_trades = [t for t in trades if t["profitAndLoss"] is not None]
    assert exit_trades, trades
    assert abs(float(exit_trades[0]["profitAndLoss"]) - 10.0) < 1e-6


def test_state_persists_across_restart(tmp_path, env):
    bar1 = Bar(ts="2026-01-01T00:00:00+00:00", o=100, h=100, l=100, c=100, v=1)
    bar2 = Bar(ts="2026-01-01T00:05:00+00:00", o=100, h=111, l=99, c=110, v=1)
    feed = StepBarFeed({("MES", "1m"): [bar1, bar2]}, visible_bars=1)

    state_path = tmp_path / "state.json"
    broker1 = SimBroker(state_path=str(state_path), bar_feed=feed)
    broker1.handle(
        "/api/Order/place",
        {
            "accountId": 900001,
            "contractId": "CON.F.US.MES.H26",
            "type": ORDER_TYPE_MARKET,
            "side": ORDER_SIDE_BID,
            "size": 1,
        },
    )
    feed.advance(1)
    broker1.sim_update(900001, "CON.F.US.MES.H26")

    # New instance reads existing state
    broker2 = SimBroker(state_path=str(state_path), bar_feed=feed)
    trades2 = broker2.handle(
        "/api/Trade/search",
        {"accountId": 900001, "startTimestamp": "2025-12-31T00:00:00+00:00"},
    )["trades"]
    assert len(trades2) >= 2
