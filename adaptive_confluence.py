import json
import logging
import os
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def filter_learning_series(series: Optional[pd.Series], side: str) -> pd.Series:
    """Return side-appropriate, clipped samples for adaptive learning."""

    if series is None:
        return pd.Series(dtype=float)

    filtered = series.dropna().clip(lower=-3.0, upper=3.0)

    if side == "BUY":
        filtered = filtered[filtered <= 0.0]
    elif side == "SELL":
        filtered = filtered[filtered >= 0.0]
    else:
        return pd.Series(dtype=float)

    return filtered


@dataclass
class AdaptiveConfluenceParams:
    """Manage adaptive confluence tuning parameters with persistence."""

    sell_zone: Tuple[float, float, float] = (-0.6, 0.8, 0.2)
    buy_zone: Tuple[float, float, float] = (-0.8, 0.2, -0.3)
    threshold: float = 1.0
    alpha: float = 0.10
    n: int = 120
    min_samples: int = 50
    save_path: str = os.path.join(".", "data", "adaptive_confluence.json")
    target_trades_per_hour: float = 1.5

    @classmethod
    def load(cls, path: Optional[str] = None) -> "AdaptiveConfluenceParams":
        path = path or os.path.join(".", "data", "adaptive_confluence.json")
        try:
            with open(path, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
            logger.info("Loaded adaptive confluence params from %s", path)
            return cls(**raw)
        except Exception as exc:
            logger.warning("Using default adaptive confluence params (%s): %s", path, exc)
            return cls(save_path=path)

    def save(self, path: Optional[str] = None) -> None:
        path = path or self.save_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(asdict(self), fp, indent=2)
        logger.info("Adaptive confluence params saved to %s", path)

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    def _smooth_zone(self, current: Tuple[float, float, float], estimate: Tuple[float, float, float]) -> Tuple[float, float, float]:
        lower_new = (1 - self.alpha) * current[0] + self.alpha * estimate[0]
        upper_new = (1 - self.alpha) * current[1] + self.alpha * estimate[1]
        sweet_new = (1 - self.alpha) * current[2] + self.alpha * estimate[2]

        lower_new = self._clamp(lower_new, -2.0, 0.2)
        upper_new = self._clamp(upper_new, -0.2, 2.0)
        sweet_new = self._clamp(sweet_new, -1.0, 1.0)

        if upper_new < lower_new:
            lower_new, upper_new = upper_new, lower_new

        return (float(lower_new), float(upper_new), float(sweet_new))

    def _quantiles(self, series: pd.Series) -> Tuple[float, float, float]:
        q = series.quantile([0.2, 0.5, 0.8])
        return (float(q.loc[0.2]), float(q.loc[0.8]), float(q.loc[0.5]))

    def update_from_series(self, series: Optional[pd.Series], side: str) -> bool:
        if series is None:
            return False

        filtered = filter_learning_series(series, side)
        if len(filtered) < self.min_samples:
            return False

        lower_est, upper_est, sweet_est = self._quantiles(filtered.tail(self.n))
        estimate = (lower_est, upper_est, sweet_est)

        if side == "SELL":
            old_zone = self.sell_zone
            new_zone = self._smooth_zone(old_zone, estimate)
            self.sell_zone = new_zone
        elif side == "BUY":
            old_zone = self.buy_zone
            new_zone = self._smooth_zone(old_zone, estimate)
            self.buy_zone = new_zone
        else:
            return False

        logger.info(
            "Adaptive params update for %s: %s -> %s (samples=%d, threshold=%.3f)",
            side,
            old_zone,
            new_zone,
            len(filtered),
            self.threshold,
        )
        return True

    def adjust_threshold(self, observed_per_hour: float) -> None:
        step = 0.02
        if observed_per_hour > self.target_trades_per_hour * 1.2:
            self.threshold = self._clamp(self.threshold + step, 0.5, 1.2)
        elif observed_per_hour < self.target_trades_per_hour * 0.8:
            self.threshold = self._clamp(self.threshold - step, 0.5, 1.2)

    def as_dict(self) -> Dict[str, float]:
        return {
            "sell_zone": self.sell_zone,
            "buy_zone": self.buy_zone,
            "threshold": self.threshold,
            "alpha": self.alpha,
            "n": self.n,
        }


def test_adaptive_params() -> None:
    params = AdaptiveConfluenceParams(alpha=0.5, n=60, min_samples=10)

    samples = pd.Series(np.linspace(-1.5, -0.3, 60))
    updated = params.update_from_series(samples, "SELL")
    assert updated is True
    lower_est, _, sweet_est = params._quantiles(samples)
    expected_lower = params._clamp(((1 - params.alpha) * -0.6) + (params.alpha * lower_est), -2.0, 0.2)
    assert np.isclose(params.sell_zone[0], expected_lower)
    assert params.sell_zone[0] <= 0.2
    assert np.isclose(params.sell_zone[2], ((1 - params.alpha) * 0.2) + (params.alpha * sweet_est))

    extreme_samples = pd.Series(np.linspace(-3.0, -2.5, 60))
    params.update_from_series(extreme_samples, "SELL")
    assert params.sell_zone[0] >= -2.0

    buy_samples = pd.Series(np.linspace(0.5, 1.5, 60))
    params.update_from_series(buy_samples, "BUY")
    assert params.buy_zone[1] <= 2.0

    tmp_path = os.path.join("/tmp", "adaptive_test.json")
    params.save(tmp_path)
    loaded = AdaptiveConfluenceParams.load(tmp_path)
    assert np.allclose(loaded.sell_zone, params.sell_zone)
    assert np.allclose(loaded.buy_zone, params.buy_zone)
    assert loaded.threshold == params.threshold

    print("Adaptive parameter tests passed")


if __name__ == "__main__":
    test_adaptive_params()
