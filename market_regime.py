import logging
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class TrendSnapshot:
    timeframe: str
    closes: List[float]
    ema_period: int
    slope_lookback: int

    def ema_series(self) -> List[float]:
        if not self.closes:
            return []
        k = 2 / (self.ema_period + 1)
        ema_values = [self.closes[0]]
        for price in self.closes[1:]:
            ema_values.append(price * k + ema_values[-1] * (1 - k))
        return ema_values

    def slope(self) -> Optional[float]:
        ema_values = self.ema_series()
        if len(ema_values) <= self.slope_lookback:
            return None
        return ema_values[-1] - ema_values[-(self.slope_lookback + 1)]


class MarketRegime:
    """Lightweight EMA21 slope detector replacing the prior regime stack."""

    def __init__(self, ema_period: int = 21, slope_lookback: int = 5, slope_threshold: float = 0.0):
        self.logger = logging.getLogger(__name__)
        self.ema_period = ema_period
        self.slope_lookback = slope_lookback
        self.slope_threshold = slope_threshold

    def analyze_regime(self, timeframe_data: Dict[str, Dict]) -> Dict:
        try:
            if not timeframe_data:
                return self._fallback("No timeframe data provided")

            tf_name, tf_payload = next(iter(timeframe_data.items()))
            closes = tf_payload.get("close", [])
            snapshot = TrendSnapshot(
                timeframe=tf_name,
                closes=closes,
                ema_period=self.ema_period,
                slope_lookback=self.slope_lookback,
            )

            slope_value = snapshot.slope()
            if slope_value is None:
                return self._fallback("Insufficient candles for EMA slope")

            direction = "trending_up" if slope_value > self.slope_threshold else "trending_down" if slope_value < -self.slope_threshold else "sideways"
            confidence = min(100, int(abs(slope_value) / max(closes[-1], 1e-6) * 10_000))
            trade_ok = direction in {"trending_up", "trending_down"} and confidence > 5

            regime = {
                "primary_regime": direction,
                "confidence": confidence,
                "supporting_factors": [
                    f"EMA{self.ema_period} slope over last {self.slope_lookback} {tf_name} bars: {slope_value:.4f}",
                ],
                "trade_recommendation": trade_ok,
                "risk_level": "medium" if trade_ok else "high",
                "trend_details": {
                    "timeframe": tf_name,
                    "ema_period": self.ema_period,
                    "slope": slope_value,
                    "slope_lookback": self.slope_lookback,
                    "current_price": closes[-1] if closes else None,
                },
                "volatility_details": {
                    "volatility_regime": "unknown",
                    "reason": "Volatility modelling removed",
                },
            }
            return regime
        except Exception as exc:
            self.logger.error("Trend slope analysis failed: %s", exc, exc_info=True)
            return self._fallback(f"Analysis error: {exc}")

    def _fallback(self, reason: str) -> Dict:
        return {
            "primary_regime": "sideways",
            "confidence": 0,
            "supporting_factors": [reason],
            "trade_recommendation": False,
            "risk_level": "high",
            "trend_details": {},
            "volatility_details": {},
        }

    def get_regime_trading_rules(self, regime: str) -> Dict:
        bias = "BUY" if regime == "trending_up" else "SELL" if regime == "trending_down" else "HOLD"
        return {
            "bias": bias,
            "notes": f"EMA{self.ema_period} slope suggests {regime}",
        }
