import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from api import get_supabase_client
from config import load_config

config = load_config()
SLOPE_LOOKBACK = config.get("SLOPE_LOOKBACK", 10)
SLOPE_THRESHOLD = config.get("SLOPE_THRESHOLD", 0.00003)
MARKET_SYMBOL = config.get("MARKET_SYMBOL", "MES")

logger = logging.getLogger(__name__)


def _aggregate_bars(minute_bars: List[Dict], interval: int) -> List[Dict]:
    if not minute_bars or interval <= 0:
        return []
    aggregated = []
    for idx in range(0, len(minute_bars), interval):
        chunk = minute_bars[idx: idx + interval]
        if len(chunk) < interval:
            continue
        aggregated.append(
            {
                "ts": chunk[-1]["ts"],
                "o": float(chunk[0]["o"]),
                "h": max(float(b["h"]) for b in chunk),
                "l": min(float(b["l"]) for b in chunk),
                "c": float(chunk[-1]["c"]),
                "v": sum(float(b.get("v", 0)) for b in chunk),
            }
        )
    return aggregated


def _ema(values: List[float], period: int = 21) -> List[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    ema_values: List[float] = []
    for idx, price in enumerate(values):
        if idx == 0:
            ema_values.append(price)
        else:
            ema_values.append((price - ema_values[-1]) * multiplier + ema_values[-1])
    return ema_values


def _linreg_slope(values: List[float]) -> Optional[float]:
    n = len(values)
    if n < 2:
        return None
    x_vals = list(range(n))
    x_mean = sum(x_vals) / n
    y_mean = sum(values) / n
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, values))
    denominator = sum((x - x_mean) ** 2 for x in x_vals)
    if denominator == 0:
        return None
    return numerator / denominator


def _normalized_slope(ema_values: List[float], current_price: float, lookback: int) -> Optional[float]:
    if current_price is None or current_price == 0:
        return None
    if len(ema_values) < lookback:
        return None
    slope = _linreg_slope(ema_values[-lookback:])
    if slope is None:
        return None
    return slope / current_price


def _determine_regime(slope_15m: Optional[float], slope_30m: Optional[float]):
    if slope_15m is None or slope_30m is None:
        return "unknown", "Missing slope data"

    abs_15 = abs(slope_15m)
    abs_30 = abs(slope_30m)

    if abs_15 < SLOPE_THRESHOLD and abs_30 < SLOPE_THRESHOLD:
        return "range", "Both slopes within threshold"
    if slope_15m > SLOPE_THRESHOLD and slope_30m > SLOPE_THRESHOLD:
        return "trend_up", "Aligned upward slopes"
    if slope_15m < -SLOPE_THRESHOLD and slope_30m < -SLOPE_THRESHOLD:
        return "trend_down", "Aligned downward slopes"
    return "mixed", "Mixed slope signals"


def build_market_state(supabase_client=None, symbol: str = MARKET_SYMBOL) -> Dict:
    client = supabase_client or get_supabase_client()
    try:
        result = (
            client.table("tv_datafeed")
            .select("o, h, l, c, v, ts")
            .eq("symbol", symbol)
            .eq("timeframe", 1)
            .order("ts", desc=True)
            .limit(600)
            .execute()
        )
    except Exception as exc:
        logger.error("Failed to fetch tv_datafeed: %s", exc)
        return {}

    rows = result.data or []
    if not rows:
        logger.warning("No tv_datafeed rows returned for symbol %s", symbol)
        return {}

    minute_bars = list(reversed(rows))
    latest_bar = minute_bars[-1]
    price = float(latest_bar.get("c"))

    aggregated = {}
    for label, interval in {"5m": 5, "15m": 15, "30m": 30}.items():
        aggregated[label] = _aggregate_bars(minute_bars, interval)

    ema21 = {}
    slopes = {}
    for label, bars in aggregated.items():
        closes = [float(bar["c"]) for bar in bars]
        ema_series = _ema(closes, period=21)
        ema21[label] = ema_series[-1] if ema_series else None
        slopes[label] = _normalized_slope(ema_series, price, SLOPE_LOOKBACK)

    regime, reason = _determine_regime(slopes.get("15m"), slopes.get("30m"))
    timestamp = latest_bar.get("ts")
    if isinstance(timestamp, str):
        ts_iso = timestamp
    else:
        ts_iso = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()

    return {
        "symbol": symbol,
        "timestamp": ts_iso,
        "price": price,
        "ema21": {tf: ema21.get(tf) for tf in ["5m", "15m", "30m"]},
        "slope": {tf: slopes.get(tf) for tf in ["5m", "15m", "30m"]},
        "regime": regime,
        "reason": reason,
    }

