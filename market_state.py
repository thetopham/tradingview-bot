"""Market state builder for reduction trigger architecture."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

from api import get_supabase_client
from config import load_config

logger = logging.getLogger(__name__)

CONFIG = load_config()
CT = CONFIG["CT"]
RECENT_BAR_LIMIT = 600
EMA_PERIOD = 21
SLOPE_WINDOW = 5
NORMALIZED_SLOPE_THRESHOLD = 0.00005
SUPPORTED_TIMEFRAMES = {
    "5m": 5,
    "15m": 15,
    "30m": 30,
}


def _fetch_recent_1m(symbol: str, limit: int = RECENT_BAR_LIMIT) -> List[Dict]:
    supabase = get_supabase_client()
    result = (
        supabase.table("tv_datafeed")
        .select("ts,o,h,l,c,v")
        .eq("symbol", symbol)
        .eq("timeframe", 1)
        .order("ts", desc=True)
        .limit(limit)
        .execute()
    )
    bars = result.data or []
    if not bars:
        logger.warning("No 1m bars returned for %s", symbol)
        return []
    sorted_bars = sorted(bars, key=lambda x: x["ts"])
    logger.info("Fetched %d 1m bars for %s", len(sorted_bars), symbol)
    return sorted_bars


def _aggregate(minute_bars: List[Dict], target_minutes: int) -> List[Dict]:
    if not minute_bars or len(minute_bars) < target_minutes:
        return []
    aggregated: List[Dict] = []
    for i in range(0, len(minute_bars) - target_minutes + 1, target_minutes):
        chunk = minute_bars[i : i + target_minutes]
        if len(chunk) < target_minutes:
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
    return aggregated[-200:]


def _ema(values: List[float], period: int = EMA_PERIOD) -> List[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    ema_values: List[float] = []
    for idx, price in enumerate(values):
        if idx == 0:
            ema_values.append(price)
            continue
        ema_values.append(alpha * price + (1 - alpha) * ema_values[-1])
    return ema_values


def _normalized_slope(series: List[float], window: int = SLOPE_WINDOW) -> Optional[float]:
    if len(series) < window:
        return None
    window_vals = series[-window:]
    y = np.array(window_vals)
    x = np.arange(len(window_vals))
    slope, _ = np.polyfit(x, y, 1)
    last = window_vals[-1]
    if last == 0:
        return 0.0
    return float(slope / last)


def _classify_regime(normalized_slope: Optional[float]) -> str:
    if normalized_slope is None:
        return "insufficient"
    if abs(normalized_slope) < NORMALIZED_SLOPE_THRESHOLD:
        return "range"
    return "trend_up" if normalized_slope > 0 else "trend_down"


def build_market_state(symbol: str) -> Dict:
    minute_bars = _fetch_recent_1m(symbol)
    state = {
        "as_of": datetime.now(CT).isoformat(),
        "symbol": symbol,
        "timeframes": {},
        "errors": [],
    }

    if not minute_bars:
        state["errors"].append("no_data")
        return state

    timeframes = {"1m": minute_bars}
    for name, minutes in SUPPORTED_TIMEFRAMES.items():
        aggregated = _aggregate(minute_bars, minutes)
        if aggregated:
            timeframes[name] = aggregated
        else:
            state["errors"].append(f"insufficient_{name}")

    for tf, bars in timeframes.items():
        closes = [float(b.get("c", 0)) for b in bars]
        ema = _ema(closes, EMA_PERIOD)
        slope = _normalized_slope(ema)
        regime = _classify_regime(slope)
        state["timeframes"][tf] = {
            "bars": bars,
            "ema21": ema,
            "normalized_slope": slope,
            "regime": regime,
            "last_close": closes[-1] if closes else None,
        }
        logger.info(
            "MarketState %s tf=%s slope=%.6f regime=%s", symbol, tf, slope or 0.0, regime
        )

    return state


__all__ = ["build_market_state", "NORMALIZED_SLOPE_THRESHOLD"]
