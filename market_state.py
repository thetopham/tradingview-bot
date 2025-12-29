import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MarketStateConfig:
    ema_period: int = 21
    slope_lookback: int = 20
    atr_period: int = 14
    deadband: float = 0.05


def _ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    ema_values = [values[0]]
    for price in values[1:]:
        ema_values.append(price * k + ema_values[-1] * (1 - k))
    return ema_values


def _atr(highs: List[float], lows: List[float], closes: List[float], period: int) -> Optional[float]:
    if not highs or not lows or not closes or len(closes) < 2:
        return None
    trs: List[float] = []
    prev_close = closes[0]
    for high, low, close in zip(highs[1:], lows[1:], closes[1:]):
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def compute_market_state(
    bars: List[Dict],
    config: MarketStateConfig = MarketStateConfig(),
) -> Dict:
    """Compute local market state from 5m OHLC bars.

    Expects `bars` as a list of dicts with keys o/h/l/c and optional ts.
    Returns a compact dict describing trend/regime, slope_norm, confidence, and signal.
    """
    if not bars or len(bars) < config.slope_lookback + 2:
        return _fallback_state("Insufficient bars for analysis")

    closes = [float(b.get("c")) for b in bars]
    highs = [float(b.get("h")) for b in bars]
    lows = [float(b.get("l")) for b in bars]
    timestamps = [b.get("ts") for b in bars]

    ema_values = _ema(closes, config.ema_period)
    if len(ema_values) <= config.slope_lookback:
        return _fallback_state("Not enough EMA values for slope")

    atr_value = _atr(highs, lows, closes, config.atr_period)
    if atr_value is None or atr_value == 0:
        return _fallback_state("ATR unavailable for normalization")

    ema_now = ema_values[-1]
    ema_then = ema_values[-(config.slope_lookback + 1)]
    slope_norm = (ema_now - ema_then) / atr_value

    if slope_norm > config.deadband:
        trend = "trending_up"
        signal = "BUY"
    elif slope_norm < -config.deadband:
        trend = "trending_down"
        signal = "SELL"
    else:
        trend = "sideways"
        signal = "HOLD"

    confidence = min(100, int(abs(slope_norm) / config.deadband * 50))
    timestamp = timestamps[-1] or datetime.now(timezone.utc).isoformat()

    supporting = [
        f"EMA{config.ema_period} slope over {config.slope_lookback} bars",
        f"ATR{config.atr_period}={atr_value:.4f}",
    ]

    state = {
        "timestamp": timestamp,
        "trend": trend,
        "regime": trend,
        "slope_norm": slope_norm,
        "confidence": confidence,
        "supporting_factors": supporting,
        "signal": signal,
    }
    logger.debug("Market state computed: %s", state)
    return state


def _fallback_state(reason: str) -> Dict:
    logger.warning("Market state fallback: %s", reason)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trend": "sideways",
        "regime": "sideways",
        "slope_norm": 0.0,
        "confidence": 0,
        "supporting_factors": [reason],
        "signal": "HOLD",
    }
