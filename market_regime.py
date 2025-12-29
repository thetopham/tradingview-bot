import logging
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class MarketState:
    regime: str
    signal: str
    slope_norm: float
    confidence: int
    timestamp: Optional[str]
    supporting_factors: List[str]
    trend_details: Dict


class MarketStateAnalyzer:
    """Compute a simple market state from 5m bars using an EMA slope normalized by ATR."""

    def __init__(
        self,
        ema_period: int = 21,
        slope_lookback: int = 20,
        atr_period: int = 14,
        deadband: float = 0.05,
    ) -> None:
        self.logger = logging.getLogger(__name__)
        self.ema_period = ema_period
        self.slope_lookback = slope_lookback
        self.atr_period = atr_period
        self.deadband = deadband

    @staticmethod
    def _ema(values: List[float], period: int) -> List[float]:
        if not values or period <= 0:
            return []
        k = 2 / (period + 1)
        ema_values = [values[0]]
        for price in values[1:]:
            ema_values.append(price * k + ema_values[-1] * (1 - k))
        return ema_values

    @staticmethod
    def _atr(bars: List[Dict], period: int) -> Optional[float]:
        if len(bars) < period + 1:
            return None
        trs: List[float] = []
        for i in range(1, len(bars)):
            high = float(bars[i].get("h", 0))
            low = float(bars[i].get("l", 0))
            prev_close = float(bars[i - 1].get("c", 0))
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        if len(trs) < period:
            return None
        initial = sum(trs[:period]) / period
        atr_values = [initial]
        alpha = 1 / period
        for tr in trs[period:]:
            atr_values.append(alpha * tr + (1 - alpha) * atr_values[-1])
        return atr_values[-1] if atr_values else None

    def compute_market_state(self, bars: List[Dict]) -> Dict:
        """Return a compact market state from a list of 5m bars (oldest -> newest)."""
        try:
            if not bars or len(bars) < max(self.ema_period, self.slope_lookback + 1):
                return self._fallback("Insufficient data for market state")

            closes = [float(bar.get("c", 0)) for bar in bars]
            ema_series = self._ema(closes, self.ema_period)
            if len(ema_series) <= self.slope_lookback:
                return self._fallback("Insufficient EMA history for slope")

            ema_now = ema_series[-1]
            ema_then = ema_series[-(self.slope_lookback + 1)]
            atr_val = self._atr(bars, self.atr_period)
            if not atr_val or atr_val == 0:
                return self._fallback("ATR unavailable for normalization")

            slope_norm = (ema_now - ema_then) / atr_val
            if slope_norm > self.deadband:
                regime = "trending_up"
                signal = "BUY"
            elif slope_norm < -self.deadband:
                regime = "trending_down"
                signal = "SELL"
            else:
                regime = "sideways"
                signal = "HOLD"

            confidence = max(0, min(100, int(abs(slope_norm) / max(self.deadband, 1e-6) * 25)))
            supporting_factors = [
                f"EMA{self.ema_period} slope over {self.slope_lookback} bars: {ema_now - ema_then:.4f}",
                f"ATR{self.atr_period}: {atr_val:.4f}",
                f"Slope norm: {slope_norm:.4f}",
            ]

            latest_ts = bars[-1].get("ts")
            trend_details = {
                "ema_period": self.ema_period,
                "ema_now": ema_now,
                "ema_then": ema_then,
                "slope_norm": slope_norm,
                "slope_raw": ema_now - ema_then,
                "slope_lookback": self.slope_lookback,
            }

            state = {
                "timestamp": latest_ts,
                "regime": regime,
                "primary_regime": regime,
                "trend": regime,
                "signal": signal,
                "slope_norm": slope_norm,
                "confidence": confidence,
                "supporting_factors": supporting_factors,
                "trade_recommendation": signal != "HOLD",
                "risk_level": "medium" if signal != "HOLD" else "high",
                "trend_details": trend_details,
            }
            return state
        except Exception as exc:
            self.logger.error("Failed to compute market state: %s", exc, exc_info=True)
            return self._fallback(f"Error computing market state: {exc}")

    @staticmethod
    def _fallback(reason: str) -> Dict:
        return {
            "timestamp": None,
            "regime": "sideways",
            "primary_regime": "sideways",
            "trend": "sideways",
            "signal": "HOLD",
            "slope_norm": 0.0,
            "confidence": 0,
            "supporting_factors": [reason],
            "trade_recommendation": False,
            "risk_level": "high",
            "trend_details": {},
        }


__all__ = ["MarketState", "MarketStateAnalyzer"]
