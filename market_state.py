import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

from config import load_config

config = load_config()
SLOPE_LOOKBACK = config.get("SLOPE_LOOKBACK", 10)
SLOPE_THRESHOLD = config.get("SLOPE_THRESHOLD", 0.00003)

logger = logging.getLogger(__name__)


def _aggregate_bars(rows: List[Dict], interval: int) -> List[Dict]:
    aggregated = []
    for i in range(0, len(rows), interval):
        chunk = rows[i:i + interval]
        if len(chunk) < interval:
            continue
        opens = chunk[0].get("open")
        closes = chunk[-1].get("close")
        high_vals = [r.get("high") for r in chunk if r.get("high") is not None]
        low_vals = [r.get("low") for r in chunk if r.get("low") is not None]
        highs = max(high_vals) if high_vals else None
        lows = min(low_vals) if low_vals else None
        volumes = sum(r.get("volume") or 0 for r in chunk)
        ts = chunk[-1].get("ts") or chunk[-1].get("timestamp")
        aggregated.append({
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
            "ts": ts,
        })
    return aggregated


def _ema(values: List[float], period: int = 21) -> List[float]:
    if not values:
        return []
    ema_values = []
    multiplier = 2 / (period + 1)
    ema_current: Optional[float] = None
    for price in values:
        if ema_current is None:
            ema_current = price
        else:
            ema_current = (price - ema_current) * multiplier + ema_current
        ema_values.append(ema_current)
    return ema_values


def _normalized_slope(series: List[float], current_price: Optional[float]) -> Optional[float]:
    if current_price in (None, 0):
        return None
    if not series or len(series) < SLOPE_LOOKBACK:
        return None
    recent = series[-SLOPE_LOOKBACK:]
    x = np.arange(len(recent))
    slope, _intercept = np.polyfit(x, recent, 1)
    return slope / current_price


def _determine_regime(slopes: Dict[str, Optional[float]]) -> (str, str):
    slope_15 = slopes.get("15m")
    slope_30 = slopes.get("30m")
    if slope_15 is None or slope_30 is None:
        return "unknown", "Missing slope data"

    abs_15 = abs(slope_15)
    abs_30 = abs(slope_30)

    if abs_15 < SLOPE_THRESHOLD and abs_30 < SLOPE_THRESHOLD:
        return "range", "Both mid/long slopes below threshold"
    if slope_15 > SLOPE_THRESHOLD and slope_30 > SLOPE_THRESHOLD:
        return "trend_up", "Both mid/long slopes above threshold"
    if slope_15 < -SLOPE_THRESHOLD and slope_30 < -SLOPE_THRESHOLD:
        return "trend_down", "Both mid/long slopes below -threshold"
    return "mixed", "Slope disagreement"


def build_market_state(supabase_client, symbol: str = "MES") -> Dict:
    try:
        response = (
            supabase_client.table("tv_datafeed")
            .select("*")
            .eq("symbol", symbol)
            .eq("timeframe", 1)
            .order("ts", desc=True)
            .limit(600)
            .execute()
        )
        rows = response.data or []
    except Exception as exc:
        logger.error("Failed to fetch tv_datafeed rows: %s", exc, exc_info=True)
        return {
            "symbol": symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": None,
            "ema21": {"5m": None, "15m": None, "30m": None},
            "slope": {"5m": None, "15m": None, "30m": None},
            "regime": "unknown",
            "reason": "supabase_fetch_failed",
        }

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

    rows = list(reversed(rows))
    closes_1m = [r.get("close") for r in rows if r.get("close") is not None]
    if not closes_1m:
        return {
            "symbol": symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": None,
            "ema21": {"5m": None, "15m": None, "30m": None},
            "slope": {"5m": None, "15m": None, "30m": None},
            "regime": "unknown",
            "reason": "no_close_prices",
        }

    price = closes_1m[-1]
    aggregated = {
        "5m": _aggregate_bars(rows, 5),
        "15m": _aggregate_bars(rows, 15),
        "30m": _aggregate_bars(rows, 30),
    }

    ema21 = {}
    slopes = {}
    for tf, bars in aggregated.items():
        closes = [b.get("close") for b in bars if b.get("close") is not None]
        ema_series = _ema(closes)
        ema_value = ema_series[-1] if ema_series else None
        ema21[tf] = ema_value
        slopes[tf] = _normalized_slope(ema_series, price)

    regime, reason = _determine_regime(slopes)

    return {
        "symbol": symbol,
        "timestamp": rows[-1].get("ts") or datetime.now(timezone.utc).isoformat(),
        "price": price,
        "ema21": ema21,
        "slope": slopes,
        "regime": regime,
        "reason": reason,
    }
