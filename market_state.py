import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from api import aggregate_1m_to_timeframe, get_supabase_client

logger = logging.getLogger(__name__)

DEFAULT_SYMBOL = "MES"
ONE_MINUTE_LIMIT = 600
EMA_PERIOD = 21
SLOPE_WINDOW = 10
TIMEFRAME_MINUTES = {
    "5m": 5,
    "15m": 15,
    "30m": 30,
}


def _fetch_minute_bars(symbol: str, limit: int = ONE_MINUTE_LIMIT) -> List[Dict]:
    supabase = get_supabase_client()
    result = (
        supabase.table("tv_datafeed")
        .select("*")
        .eq("symbol", symbol)
        .eq("timeframe", 1)
        .order("ts", desc=True)
        .limit(limit)
        .execute()
    )
    data = result.data or []
    logger.info("Fetched %s 1m bars for %s", len(data), symbol)
    return data


def _ema(values: List[float], period: int = EMA_PERIOD) -> List[float]:
    if not values:
        return []
    ema_values: List[float] = []
    multiplier = 2 / (period + 1)
    for idx, price in enumerate(values):
        if idx == 0:
            ema_values.append(price)
        else:
            ema_values.append((price - ema_values[-1]) * multiplier + ema_values[-1])
    return ema_values


def _normalized_slope(series: List[float], reference_price: float, window: int = SLOPE_WINDOW) -> Optional[float]:
    if not series or len(series) < max(3, window) or not reference_price:
        return None
    window_series = series[-window:]
    x = np.arange(len(window_series))
    y = np.array(window_series)
    slope, _ = np.polyfit(x, y, 1)
    return float(slope / reference_price)


def _summarize_timeframe(bars: List[Dict], timeframe: str) -> Dict:
    closes = [float(bar.get("c", 0)) for bar in bars]
    ema_series = _ema(closes, EMA_PERIOD)
    last_close = closes[-1] if closes else None
    slope = _normalized_slope(ema_series, last_close) if last_close else None
    return {
        "timeframe": timeframe,
        "close": closes,
        "ema21": ema_series,
        "last_close": last_close,
        "normalized_slope": slope,
    }


def build_market_state(symbol: str = DEFAULT_SYMBOL) -> Dict:
    try:
        minute_bars = _fetch_minute_bars(symbol)
        aggregated: Dict[str, List[Dict]] = {}
        for tf, minutes in TIMEFRAME_MINUTES.items():
            aggregated[tf] = aggregate_1m_to_timeframe(minute_bars, minutes)

        tf_state = {tf: _summarize_timeframe(bars, tf) for tf, bars in aggregated.items()}
        market_state = {
            "symbol": symbol,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "timeframes": tf_state,
            "source": "supabase_tv_datafeed",
            "sample_sizes": {tf: len(bars) for tf, bars in aggregated.items()},
        }

        logger.info(
            "Market state built for %s | slopes=%s",
            symbol,
            {tf: round(state.get("normalized_slope", 0) or 0, 6) for tf, state in tf_state.items()},
        )
        return market_state
    except Exception as exc:
        logger.error("Failed to build market state: %s", exc, exc_info=True)
        return {
            "symbol": symbol,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "timeframes": {},
            "source": "supabase_tv_datafeed",
            "error": str(exc),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    state = build_market_state()
    print(state)
