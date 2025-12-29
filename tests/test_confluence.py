import pandas as pd

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
