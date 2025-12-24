import logging
from typing import Dict

from config import load_config

config = load_config()
SLOPE_LOOKBACK = config.get("SLOPE_LOOKBACK", 10)
SLOPE_THRESHOLD = config.get("SLOPE_THRESHOLD", 0.00003)

logger = logging.getLogger(__name__)


def decide(market_state: Dict, position_context: Dict, in_get_flat: bool, trading_enabled: bool) -> Dict:
    if not market_state or not market_state.get("slope"):
        return {
            "action": "HOLD",
            "reason_code": "NO_DATA",
            "details": {
                "slopes": market_state.get("slope") if market_state else {},
                "threshold": SLOPE_THRESHOLD,
                "lookback": SLOPE_LOOKBACK,
            },
        }

    current_position = position_context.get("current_position", {}) if position_context else {}
    account_metrics = position_context.get("account_metrics", {}) if position_context else {}
    has_position = current_position.get("has_position", False)
    size = current_position.get("size", 0) or 0
    can_trade = account_metrics.get("can_trade", False)

    slopes = market_state.get("slope", {})
    slope_15m = slopes.get("15m")
    slope_30m = slopes.get("30m")

    if in_get_flat:
        return {
            "action": "HOLD",
            "reason_code": "GET_FLAT",
            "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK},
        }

    if not can_trade:
        return {
            "action": "HOLD",
            "reason_code": "CANT_TRADE",
            "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK},
        }

    if has_position and size > 0:
        return {
            "action": "HOLD",
            "reason_code": "HAS_POSITION",
            "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK},
        }

    if slope_15m is None or slope_30m is None:
        return {
            "action": "HOLD",
            "reason_code": "NO_DATA",
            "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK},
        }

    if abs(slope_15m) < SLOPE_THRESHOLD and abs(slope_30m) < SLOPE_THRESHOLD:
        return {
            "action": "HOLD",
            "reason_code": "RANGE",
            "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK},
        }

    if slope_15m > SLOPE_THRESHOLD and slope_30m > SLOPE_THRESHOLD:
        return {
            "action": "BUY",
            "reason_code": "TREND_UP",
            "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK},
        }

    if slope_15m < -SLOPE_THRESHOLD and slope_30m < -SLOPE_THRESHOLD:
        return {
            "action": "SELL",
            "reason_code": "TREND_DOWN",
            "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK},
        }

    return {
        "action": "HOLD",
        "reason_code": "MIXED",
        "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK},
    }

