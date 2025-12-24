import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional
import numpy as np

from api import get_supabase_client
from config import load_config

config = load_config()
SLOPE_LOOKBACK = config['SLOPE_LOOKBACK']
SLOPE_THRESHOLD = config['SLOPE_THRESHOLD']
MARKET_SYMBOL = config['MARKET_SYMBOL']

logger = logging.getLogger(__name__)


def _aggregate(minute_bars: List[Dict], interval: int) -> List[Dict]:
    if not minute_bars or interval <= 0:
        return []
    sorted_bars = sorted(minute_bars, key=lambda b: b['ts'])
    chunks = len(sorted_bars) // interval
    aggregated = []
    for idx in range(chunks):
        start = idx * interval
        end = start + interval
        chunk = sorted_bars[start:end]
        if len(chunk) < interval:
            continue
        aggregated.append({
            'ts': chunk[-1]['ts'],
            'o': float(chunk[0]['o']),
            'h': max(float(bar['h']) for bar in chunk),
            'l': min(float(bar['l']) for bar in chunk),
            'c': float(chunk[-1]['c']),
            'v': sum(float(bar.get('v', 0)) for bar in chunk),
        })
    return aggregated


def _ema(values: List[float], period: int = 21) -> List[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    ema_series = [values[0]]
    for price in values[1:]:
        ema_series.append((price - ema_series[-1]) * multiplier + ema_series[-1])
    return ema_series


def _normalized_slope(series: List[float], lookback: int, current_price: float) -> Optional[float]:
    if not series or len(series) < lookback or current_price is None:
        return None
    window = series[-lookback:]
    x = np.arange(len(window))
    slope, _ = np.polyfit(x, window, 1)
    return slope / current_price if current_price else None


def _regime_from_slopes(slopes: Dict[str, Optional[float]]) -> (str, str):
    s15 = slopes.get('15m')
    s30 = slopes.get('30m')
    if s15 is None or s30 is None:
        return 'unknown', 'insufficient slope data'
    if abs(s15) < SLOPE_THRESHOLD and abs(s30) < SLOPE_THRESHOLD:
        return 'range', '15m/30m slopes within threshold'
    if s15 > SLOPE_THRESHOLD and s30 > SLOPE_THRESHOLD:
        return 'trend_up', '15m/30m slopes rising above threshold'
    if s15 < -SLOPE_THRESHOLD and s30 < -SLOPE_THRESHOLD:
        return 'trend_down', '15m/30m slopes falling below threshold'
    return 'mixed', 'timeframes disagree on slope direction'


def build_market_state(supabase_client=None, symbol: str = MARKET_SYMBOL) -> Dict:
    supabase = supabase_client or get_supabase_client()
    try:
        result = supabase.table('tv_datafeed') \
            .select('*') \
            .eq('symbol', symbol) \
            .eq('timeframe', 1) \
            .order('ts', desc=True) \
            .limit(600) \
            .execute()
        minute_bars = result.data or []
    except Exception as exc:
        logger.error("Failed to fetch tv_datafeed bars: %s", exc)
        minute_bars = []

    if not minute_bars:
        return {
            "symbol": symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": None,
            "ema21": {"5m": None, "15m": None, "30m": None},
            "slope": {"5m": None, "15m": None, "30m": None},
            "regime": "unknown",
            "reason": "no data",
        }

    ordered = list(reversed(minute_bars))
    price = float(ordered[-1]['c']) if ordered[-1].get('c') is not None else None

    tf_map = {'5m': 5, '15m': 15, '30m': 30}
    ema_values: Dict[str, Optional[float]] = {k: None for k in tf_map}
    slopes: Dict[str, Optional[float]] = {k: None for k in tf_map}

    for tf_name, minutes in tf_map.items():
        aggregated = _aggregate(ordered, minutes)
        closes = [bar['c'] for bar in aggregated]
        ema_series = _ema(closes, period=21)
        ema_values[tf_name] = ema_series[-1] if ema_series else None
        slopes[tf_name] = _normalized_slope(ema_series, SLOPE_LOOKBACK, price)

    regime, reason = _regime_from_slopes(slopes)

    timestamp_raw = ordered[-1].get('ts')
    if isinstance(timestamp_raw, str):
        timestamp = timestamp_raw
    else:
        try:
            timestamp = datetime.fromtimestamp(float(timestamp_raw), tz=timezone.utc).isoformat()
        except Exception:
            timestamp = datetime.now(timezone.utc).isoformat()

    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "price": price,
        "ema21": ema_values,
        "slope": slopes,
        "regime": regime,
        "reason": reason,
    }
