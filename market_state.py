import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

from api import get_supabase_client
from config import load_config

config = load_config()
DEFAULT_SYMBOL = config.get("DEFAULT_SYMBOL", "MES")
CT = config.get("CT")


@dataclass
class TimeframeState:
    timeframe: str
    closes: List[float]
    ema21: List[float]
    normalized_slope: Optional[float]


@dataclass
class MarketState:
    symbol: str
    as_of: datetime
    timeframes: Dict[str, TimeframeState] = field(default_factory=dict)


EMA_PERIOD = 21
SLOPE_WINDOW = 10
TIMEFRAMES = {"5m": 5, "15m": 15, "30m": 30}
MIN_REQUIRED_BARS = 30


def fetch_recent_minute_bars(symbol: str = DEFAULT_SYMBOL, limit: int = 600) -> List[dict]:
    supabase = get_supabase_client()
    logging.info("Fetching last %s 1m bars for %s from Supabase tv_datafeed", limit, symbol)
    result = (
        supabase.table("tv_datafeed")
        .select("*")
        .eq("symbol", symbol)
        .eq("timeframe", 1)
        .order("ts", desc=True)
        .limit(limit)
        .execute()
    )
    bars = result.data or []
    bars.sort(key=lambda bar: bar.get("ts"))
    return bars


def aggregate_bars(minute_bars: List[dict], interval_minutes: int) -> List[dict]:
    aggregated = []
    bucket = []
    for bar in minute_bars:
        bucket.append(bar)
        if len(bucket) == interval_minutes:
            open_price = bucket[0]["o"]
            high_price = max(b["h"] for b in bucket)
            low_price = min(b["l"] for b in bucket)
            close_price = bucket[-1]["c"]
            volume = sum(b.get("v", 0) for b in bucket)
            aggregated.append({
                "o": open_price,
                "h": high_price,
                "l": low_price,
                "c": close_price,
                "v": volume,
                "ts": bucket[-1].get("ts"),
            })
            bucket = []
    return aggregated


def compute_ema(values: List[float], period: int = EMA_PERIOD) -> List[float]:
    if not values:
        return []
    ema_values = []
    alpha = 2 / (period + 1)
    ema_prev = values[0]
    ema_values.append(ema_prev)
    for price in values[1:]:
        ema_prev = (price - ema_prev) * alpha + ema_prev
        ema_values.append(ema_prev)
    return ema_values


def compute_normalized_slope(series: List[float], window: int = SLOPE_WINDOW) -> Optional[float]:
    if not series:
        return None
    window = min(window, len(series))
    y = np.array(series[-window:])
    x = np.arange(window)
    try:
        slope, _ = np.polyfit(x, y, 1)
    except Exception:
        return None
    last_price = y[-1]
    if not last_price:
        return None
    return float(slope / last_price)


def build_market_state(symbol: str = DEFAULT_SYMBOL) -> MarketState:
    minute_bars = fetch_recent_minute_bars(symbol)
    timeframe_states: Dict[str, TimeframeState] = {}

    for tf_name, tf_minutes in TIMEFRAMES.items():
        aggregated = aggregate_bars(minute_bars, tf_minutes)
        closes = [bar["c"] for bar in aggregated]
        if len(closes) < MIN_REQUIRED_BARS:
            logging.warning("Insufficient %s bars (%s) for %s", tf_name, len(closes), symbol)
            continue
        ema21 = compute_ema(closes, EMA_PERIOD)
        slope = compute_normalized_slope(ema21)
        timeframe_states[tf_name] = TimeframeState(
            timeframe=tf_name,
            closes=closes,
            ema21=ema21,
            normalized_slope=slope,
        )
        logging.info(
            "MarketState %s: closes=%s ema_tail=%.2f slope=%.5f",
            tf_name,
            len(closes),
            ema21[-1] if ema21 else float("nan"),
            slope if slope is not None else float("nan"),
        )

    return MarketState(symbol=symbol, as_of=datetime.now(CT), timeframes=timeframe_states)
