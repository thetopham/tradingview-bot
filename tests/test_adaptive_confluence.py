import os
import sys

import numpy as np
import pandas as pd

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("ACCOUNT_TEST", "1")

import api
from adaptive_confluence import AdaptiveConfluenceParams


def test_buy_does_not_learn_from_positive_only_series():
    params = AdaptiveConfluenceParams()
    initial_buy_zone = params.buy_zone

    series = pd.Series(np.linspace(0.2, 1.5, 120))
    updated = params.update_from_series(series, "BUY")

    assert updated is False
    assert params.buy_zone == initial_buy_zone


def test_sell_does_not_learn_from_negative_only_series():
    params = AdaptiveConfluenceParams()
    initial_sell_zone = params.sell_zone

    series = pd.Series(np.linspace(-1.5, -0.2, 120))
    updated = params.update_from_series(series, "SELL")

    assert updated is False
    assert params.sell_zone == initial_sell_zone


def test_buy_learns_from_negative_half_of_mixed_series():
    params = AdaptiveConfluenceParams(alpha=0.5, n=120, min_samples=10)
    series = pd.Series(np.linspace(-1.5, 1.5, 240))

    updated = params.update_from_series(series, "BUY")

    assert updated is True
    assert params.buy_zone[2] < 0  # sweet spot stays below EMA
    assert params.buy_zone[1] <= 0.3  # upper bound should not drift far above zero


def test_sell_learns_from_positive_half_of_mixed_series():
    params = AdaptiveConfluenceParams(alpha=0.5, n=120, min_samples=10)
    series = pd.Series(np.linspace(-1.5, 1.5, 240))

    updated = params.update_from_series(series, "SELL")

    assert updated is True
    assert params.sell_zone[2] > 0  # sweet spot stays above EMA
    assert params.sell_zone[0] >= -0.3  # lower bound should not drift deeply negative


def test_sideways_regime_skips_adaptive_update():
    params = AdaptiveConfluenceParams(alpha=0.5, n=120, min_samples=10)
    api._adaptive_params = params

    data = []
    price = 100.0
    for i in range(150):
        price += 0.1
        data.append(
            {
                "ts": f"2024-01-01T00:{i:02d}:00",
                "o": price - 0.1,
                "h": price + 0.2,
                "l": price - 0.2,
                "c": price,
                "atr": 1.0,
            }
        )
    df = pd.DataFrame(data)

    initial_buy_zone = params.buy_zone
    try:
        updated_params, sample_count = api._update_adaptive_params(
            df, {"signal": "BUY", "regime": "sideways"}
        )
    finally:
        api._adaptive_params = None

    assert sample_count > 0
    assert updated_params.buy_zone == initial_buy_zone
