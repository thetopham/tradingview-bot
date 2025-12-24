import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

from api import aggregate_1m_to_timeframe, get_supabase_client

EMA_PERIOD = 21
SLOPE_WINDOW = 5
TIMEFRAME_MAP = {
    "5m": 5,
    "15m": 15,
    "30m": 30,
}


@dataclass
class TimeframeState:
    timeframe: str
    bars: List[Dict]
    ema21: List[float]
    normalized_slope: float


@dataclass
class MarketState:
    symbol: str
    as_of: str
    timeframes: Dict[str, TimeframeState]


def _compute_ema(values: List[float], period: int = EMA_PERIOD) -> List[float]:
    if not values:
        return []
    ema_values = []
    multiplier = 2 / (period + 1)

    for idx, price in enumerate(values):
        if idx == 0:
            ema_values.append(price)
            continue
        prev_ema = ema_values[-1]
        ema_values.append((price - prev_ema) * multiplier + prev_ema)
    return ema_values


def _compute_normalized_slope(series: List[float], price: Optional[float]) -> float:
    if not series or price in (0, None):
        return 0.0
    if len(series) < 2:
        return 0.0

    y = np.array(series)
    x = np.arange(len(y))
    slope, _ = np.polyfit(x, y, 1)
    return float(slope / price) if price else 0.0


def _summarize_state(tf: str, bars: List[Dict]) -> TimeframeState:
    closes = [float(bar.get("c", 0)) for bar in bars]
    ema = _compute_ema(closes, EMA_PERIOD)
    slope_slice = ema[-SLOPE_WINDOW:] if ema else []
    last_price = closes[-1] if closes else 0
    normalized_slope = _compute_normalized_slope(slope_slice, last_price)

    return TimeframeState(
        timeframe=tf,
        bars=bars,
        ema21=ema,
        normalized_slope=normalized_slope,
    )


def build_market_state(symbol: str, supabase_client=None, bars: int = 600) -> Optional[MarketState]:
    """
    Fetch 1m bars from Supabase, aggregate to higher timeframes, and compute EMA slopes.
    Returns a MarketState dataclass for downstream decision logic.
    """
    supabase = supabase_client or get_supabase_client()
    try:
        result = (
            supabase
            .table("tv_datafeed")
            .select("o,h,l,c,v,ts")
            .eq("symbol", symbol)
            .eq("timeframe", 1)
            .order("ts", desc=True)
            .limit(bars)
            .execute()
        )
        minute_bars = list(reversed(result.data or []))
    except Exception as exc:
        logging.error("Failed to fetch tv_datafeed bars: %s", exc)
        return None

    if not minute_bars:
        logging.warning("No 1m bars returned for symbol %s", symbol)
        return None

    timeframe_states: Dict[str, TimeframeState] = {}
    for tf, minutes in TIMEFRAME_MAP.items():
        aggregated = aggregate_1m_to_timeframe(minute_bars, minutes)
        if not aggregated:
            logging.warning("No aggregated bars for %s timeframe", tf)
            continue
        timeframe_states[tf] = _summarize_state(tf, aggregated)

    market_state = MarketState(
        symbol=symbol,
        as_of=datetime.now(timezone.utc).isoformat(),
        timeframes=timeframe_states,
    )

    try:
        payload = {
            tf: {
                "bars": len(state.bars),
                "ema_points": len(state.ema21),
                "normalized_slope": state.normalized_slope,
            }
            for tf, state in timeframe_states.items()
        }
        logging.info("Market state summary: %s", json.dumps(payload))
    except Exception:
        logging.debug("Unable to serialize market state summary", exc_info=True)

    return market_state
