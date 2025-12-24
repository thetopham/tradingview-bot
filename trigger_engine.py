import logging
from typing import Dict

from config import load_config

config = load_config()
SLOPE_LOOKBACK = config.get("SLOPE_LOOKBACK", 10)
SLOPE_THRESHOLD = config.get("SLOPE_THRESHOLD", 0.00003)

logger = logging.getLogger(__name__)


def decide(market_state: Dict, position_context: Dict, in_get_flat: bool, trading_enabled: bool) -> Dict:
    slopes = (market_state or {}).get("slope", {})
    action = "HOLD"
    reason_code = "NO_DATA"

    account_metrics = (position_context or {}).get("account_metrics", {})
    current_position = (position_context or {}).get("current_position", {})

    if in_get_flat:
        return {
            "action": "HOLD",
            "reason_code": "GET_FLAT",
            "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK},
        }

    if account_metrics and account_metrics.get("can_trade") is False:
        return {
            "action": "HOLD",
            "reason_code": "CANT_TRADE",
            "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK},
        }

    if current_position and current_position.get("has_position") and current_position.get("size", 0) > 0:
        return {
            "action": "HOLD",
            "reason_code": "HAS_POSITION",
            "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK},
        }

    slope_15 = slopes.get("15m")
    slope_30 = slopes.get("30m")

    if slope_15 is None or slope_30 is None:
        return {
            "action": "HOLD",
            "reason_code": "NO_DATA",
            "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK},
        }

    if abs(slope_15) < SLOPE_THRESHOLD and abs(slope_30) < SLOPE_THRESHOLD:
        action = "HOLD"
        reason_code = "RANGE"
    elif slope_15 > SLOPE_THRESHOLD and slope_30 > SLOPE_THRESHOLD:
        action = "BUY"
        reason_code = "TREND_UP"
    elif slope_15 < -SLOPE_THRESHOLD and slope_30 < -SLOPE_THRESHOLD:
        action = "SELL"
        reason_code = "TREND_DOWN"
    else:
        action = "HOLD"
        reason_code = "MIXED"

    return {
        "action": action,
        "reason_code": reason_code,
        "details": {"slopes": slopes, "threshold": SLOPE_THRESHOLD, "lookback": SLOPE_LOOKBACK, "trading_enabled": trading_enabled},
    }
