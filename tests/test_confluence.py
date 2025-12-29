import os
import sys
from unittest.mock import patch

import pandas as pd

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from confluence import compute_confluence


def _base_df():
    data = []
    price = 100.0
    for i in range(30):
        price += 0.1
        data.append(
            {
                "ts": f"2024-01-01T00:{i:02d}:00",
                "o": price - 0.2,
                "h": price + 0.2,
                "l": price - 0.4,
                "c": price,
                "v": 1000 + i,
                "ema21": price - 0.1,
                "atr": 1.0,
            }
        )
    return pd.DataFrame(data)


def _channel_df(close_last: float, ema_val: float = 100.0):
    data = []
    base_price = 100.0
    for i in range(10):
        price = base_price - i * 0.2
        if i == 9:
            price = close_last
        data.append(
            {
                "ts": f"2024-01-01T00:{i:02d}:00",
                "o": price,
                "h": price + 0.2,
                "l": price - 0.2,
                "c": price,
                "v": 1000,
                "ema21": ema_val,
                "atr": 1.0,
            }
        )
    return pd.DataFrame(data)


def test_pullback_zone_triggers_signal():
    df = _base_df()
    df.loc[df.index[-1], "c"] = df.loc[df.index[-1], "ema21"] - 0.2  # within BUY pullback window

    result = compute_confluence(df, base_signal="BUY")["confluence"]
    pullback = next(c for c in result["components"] if c["name"] == "pullback_to_mean")

    assert pullback["signal"] == 1
    assert pullback["confidence"] > 0


def test_channel_break_blocks_trendline_gate():
    data = []
    for i in range(13):
        base = 100
        data.append(
            {
                "ts": f"2024-01-01T00:{i:02d}:00",
                "o": base,
                "h": base + (2 if i % 2 else 1),
                "l": base - (2 if i % 2 else 1),
                "c": base + (10 if i == 12 else 0.2),
                "v": 1000,
                "atr": 1.0,
            }
        )
    df = pd.DataFrame(data)
    result = compute_confluence(df, base_signal="SELL")["confluence"]

    assert result["gates"]["trendline_ok"] is False
    trend = next(c for c in result["components"] if c["name"] == "trend_channel")
    assert trend["signal"] == 0


def test_trend_continuation_sell_triggered_within_atr():
    pivots = {"highs": [{"x": 0, "y": 101}, {"x": 5, "y": 100}], "lows": [{"x": 0, "y": 99}, {"x": 5, "y": 98}]}
    close_last = 96.8  # dist_to_lower_atr = -0.4
    df = _channel_df(close_last)

    with patch("confluence._detect_pivots", return_value=pivots):
        result = compute_confluence(
            df,
            base_signal="SELL",
            market_state={"signal": "SELL", "confidence": 100},
        )["confluence"]

    assert result["trade_recommended"] is True
    assert result["bias"] in {"SELL", "HOLD"}
    trend = next(c for c in result["components"] if c["name"] == "trend_channel")
    assert trend["signal"] == -1
    assert "trend_continuation" in trend.get("tags", [])


def test_trend_continuation_rejected_when_too_extended():
    pivots = {"highs": [{"x": 0, "y": 101}, {"x": 5, "y": 100}], "lows": [{"x": 0, "y": 99}, {"x": 5, "y": 98}]}
    close_last = 95.7  # dist_to_lower_atr = -1.5
    df = _channel_df(close_last)

    with patch("confluence._detect_pivots", return_value=pivots):
        result = compute_confluence(
            df,
            base_signal="SELL",
            market_state={"signal": "SELL", "confidence": 100},
        )["confluence"]

    assert result["trade_recommended"] is False
    trend = next(c for c in result["components"] if c["name"] == "trend_channel")
    assert trend["signal"] == 0
    assert "too_extended" in trend.get("tags", [])


def test_strong_trend_continuation_allows_trade_when_score_low():
    pivots = {"highs": [{"x": 0, "y": 101}, {"x": 5, "y": 100}], "lows": [{"x": 0, "y": 99}, {"x": 5, "y": 98}]}
    close_last = 96.4  # dist_to_lower_atr = -0.8, score below threshold
    df = _channel_df(close_last)

    with patch("confluence._detect_pivots", return_value=pivots):
        result = compute_confluence(
            df,
            base_signal="SELL",
            market_state={"signal": "SELL", "confidence": 100},
        )["confluence"]

    assert result["trade_recommended"] is True
    assert abs(result["score"]) < 1.0
    trend = next(c for c in result["components"] if c["name"] == "trend_channel")
    assert "trend_continuation" in trend.get("tags", [])


def _sideways_df(z_val: float) -> pd.DataFrame:
    base_price = 100.0
    atr = 1.0
    rows = []
    for i in range(25):
        price = base_price + (0.05 if i % 2 else -0.05)
        rows.append(
            {
                "ts": f"2024-01-01T00:{i:02d}:00",
                "o": price,
                "h": price + 0.6,
                "l": price - 0.6,
                "c": price,
                "ema21": price,
                "vwap": price,
                "atr": atr,
                "bb_upper": price + 1.0,
                "bb_lower": price - 1.0,
                "bb_middle": price,
            }
        )

    close = base_price + z_val * atr
    rows[-1]["c"] = close
    rows[-1]["o"] = close - (0.4 if z_val < 0 else -0.4)
    rows[-1]["l"] = close - (0.8 if z_val < 0 else 0.4)
    rows[-1]["h"] = close + (0.2 if z_val < 0 else 1.0)
    return pd.DataFrame(rows)


def test_sideways_scalp_buy_triggers_with_rejection():
    df = _sideways_df(-0.9)
    market_state = {"regime": "sideways", "signal": "HOLD", "slope_norm": 0.0, "deadband": 0.05}

    result = compute_confluence(df, market_state=market_state)["confluence"]

    assert result["bias"] == "BUY"
    assert result["trade_recommended"] is True
    assert any("scalp" in t for t in result.get("tags", []))


def test_sideways_scalp_sell_triggers_with_rejection():
    df = _sideways_df(0.95)
    market_state = {"regime": "sideways", "signal": "HOLD", "slope_norm": 0.0, "deadband": 0.05}

    result = compute_confluence(df, market_state=market_state)["confluence"]

    assert result["bias"] == "SELL"
    assert result["trade_recommended"] is True
    assert any("scalp" in t for t in result.get("tags", []))


def test_scalp_not_active_in_trend():
    df = _sideways_df(-0.9)
    market_state = {"regime": "trending_up", "signal": "BUY", "slope_norm": 0.2, "deadband": 0.05}

    result = compute_confluence(df, market_state=market_state)["confluence"]

    assert result["bias"] in {"BUY", "HOLD"}
    assert not any("scalp" in t for t in result.get("tags", []))
