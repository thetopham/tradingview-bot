from typing import Dict

from config import load_config

config = load_config()
SLOPE_LOOKBACK = config.get("SLOPE_LOOKBACK", 10)
SLOPE_THRESHOLD = config.get("SLOPE_THRESHOLD", 0.00003)


def decide(market_state: Dict, position_context: Dict, in_get_flat: bool, trading_enabled: bool) -> Dict:
    slopes = (market_state or {}).get("slope", {}) or {}
    details = {
        "slopes": slopes,
        "threshold": SLOPE_THRESHOLD,
        "lookback": SLOPE_LOOKBACK,
    }

    if in_get_flat:
        return {"action": "HOLD", "reason_code": "GET_FLAT", "details": details}

    account_metrics = (position_context or {}).get("account_metrics", {})
    if account_metrics is not None and not account_metrics.get("can_trade", True):
        return {"action": "HOLD", "reason_code": "CANT_TRADE", "details": details}

    current_position = (position_context or {}).get("current_position", {})
    if current_position.get("has_position") and current_position.get("size", 0) != 0:
        return {"action": "HOLD", "reason_code": "HAS_POSITION", "details": details}

    slope_15 = slopes.get("15m")
    slope_30 = slopes.get("30m")

    if slope_15 is None or slope_30 is None:
        return {"action": "HOLD", "reason_code": "NO_DATA", "details": details}

    if abs(slope_15) < SLOPE_THRESHOLD and abs(slope_30) < SLOPE_THRESHOLD:
        return {"action": "HOLD", "reason_code": "RANGE", "details": details}

    if slope_15 > SLOPE_THRESHOLD and slope_30 > SLOPE_THRESHOLD:
        return {"action": "BUY", "reason_code": "TREND_UP", "details": details}

    if slope_15 < -SLOPE_THRESHOLD and slope_30 < -SLOPE_THRESHOLD:
        return {"action": "SELL", "reason_code": "TREND_DOWN", "details": details}

    return {"action": "HOLD", "reason_code": "MIXED", "details": details}
