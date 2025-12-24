import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import load_config

config = load_config()
SLOPE_LOOKBACK = config.get("SLOPE_LOOKBACK", 10)
SLOPE_THRESHOLD = config.get("SLOPE_THRESHOLD", 0.00003)
MARKET_SYMBOL = config.get("MARKET_SYMBOL", "MES")


def _compute_ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    ema = [values[0]]
    for price in values[1:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema


def _linreg_slope(values: List[float]) -> Optional[float]:
    n = len(values)
    if n < 2:
        return None
    x_values = list(range(n))
    sum_x = sum(x_values)
    sum_y = sum(values)
    sum_xy = sum(x * y for x, y in zip(x_values, values))
    sum_x2 = sum(x * x for x in x_values)
    denominator = n * sum_x2 - sum_x ** 2
    if denominator == 0:
        return None
    return (n * sum_xy - sum_x * sum_y) / denominator


def _normalized_slope(series: List[float], price: Optional[float], lookback: int) -> Optional[float]:
    if price in (None, 0) or not series or len(series) < lookback:
        return None
    window = series[-lookback:]
    slope = _linreg_slope(window)
    if slope is None:
        return None
    return slope / price


def _aggregate_bars(rows: List[Dict], window: int) -> List[Dict]:
    aggregated = []
    for idx in range(0, len(rows), window):
        chunk = rows[idx:idx + window]
        if len(chunk) < window:
            continue
        opens = chunk[0].get("open")
        highs = max(bar.get("high") for bar in chunk)
        lows = min(bar.get("low") for bar in chunk)
        closes = chunk[-1].get("close")
        volumes = sum(bar.get("volume", 0) for bar in chunk)
        ts = chunk[-1].get("ts")
        aggregated.append({
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "ts": ts,
        })
    return aggregated


def _extract_close_series(bars: List[Dict]) -> List[float]:
    closes = []
    for bar in bars:
        close = bar.get("close")
        if close is None:
            continue
        try:
            closes.append(float(close))
        except (TypeError, ValueError):
            continue
    return closes


def build_market_state(supabase_client, symbol: str = MARKET_SYMBOL) -> Dict:
    """Build market state from Supabase 1m OHLCV rows."""
    try:
        result = (
            supabase_client
            .table('tv_datafeed')
            .select('*')
            .eq('symbol', symbol)
            .eq('timeframe', 1)
            .order('ts', desc=True)
            .limit(600)
            .execute()
        )
    except Exception as exc:
        logging.error("Failed to fetch tv_datafeed rows: %s", exc)
        return {
            "symbol": symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": None,
            "ema21": {"5m": None, "15m": None, "30m": None},
            "slope": {"5m": None, "15m": None, "30m": None},
            "regime": "unknown",
            "reason": "supabase_error",
        }

    rows = result.data or []
    if not rows:
        return {
            "symbol": symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": None,
            "ema21": {"5m": None, "15m": None, "30m": None},
            "slope": {"5m": None, "15m": None, "30m": None},
            "regime": "unknown",
            "reason": "no_data",
        }

    # Ensure oldest -> newest order
    rows = list(reversed(rows))

    # Normalize numeric fields
    for bar in rows:
        for field in ("open", "high", "low", "close", "volume"):
            if field in bar and bar[field] is not None:
                try:
                    bar[field] = float(bar[field])
                except (TypeError, ValueError):
                    bar[field] = None

    current_price = rows[-1].get("close")

    tf_map = {"5m": 5, "15m": 15, "30m": 30}
    ema21_values = {}
    slopes = {}

    for label, minutes in tf_map.items():
        aggregated = _aggregate_bars(rows, minutes)
        closes = _extract_close_series(aggregated)
        ema_series = _compute_ema(closes, 21) if closes else []
        ema21_values[label] = ema_series[-1] if len(ema_series) >= 21 else None
        slopes[label] = _normalized_slope(ema_series, current_price, SLOPE_LOOKBACK)

    slope_15 = slopes.get("15m")
    slope_30 = slopes.get("30m")
    regime = "unknown"
    reason = "insufficient_data"

    if slope_15 is not None and slope_30 is not None:
        if abs(slope_15) < SLOPE_THRESHOLD and abs(slope_30) < SLOPE_THRESHOLD:
            regime = "range"
            reason = "both_slopes_within_threshold"
        elif slope_15 > SLOPE_THRESHOLD and slope_30 > SLOPE_THRESHOLD:
            regime = "trend_up"
            reason = "slopes_bullish"
        elif slope_15 < -SLOPE_THRESHOLD and slope_30 < -SLOPE_THRESHOLD:
            regime = "trend_down"
            reason = "slopes_bearish"
        else:
            regime = "mixed"
            reason = "slopes_disagree"

    return {
        "symbol": symbol,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "price": current_price,
        "ema21": ema21_values,
        "slope": slopes,
        "regime": regime,
        "reason": reason,
    }
