import logging
from typing import Dict

from config import load_config

config = load_config()
SLOPE_THRESHOLD = config['SLOPE_THRESHOLD']
SLOPE_LOOKBACK = config['SLOPE_LOOKBACK']

logger = logging.getLogger(__name__)


def decide(market_state: Dict, position_context: Dict, in_get_flat: bool, trading_enabled: bool) -> Dict:
    slopes = market_state.get('slope', {}) if market_state else {}
    action_plan = {
        "action": "HOLD",
        "reason_code": "NO_DATA",
        "details": {
            "slopes": slopes,
            "threshold": SLOPE_THRESHOLD,
            "lookback": SLOPE_LOOKBACK,
        },
    }

    if in_get_flat:
        action_plan.update({"action": "HOLD", "reason_code": "GET_FLAT"})
        return action_plan

    account_metrics = position_context.get('account_metrics', {}) if position_context else {}
    if not account_metrics.get('can_trade', True):
        action_plan.update({"action": "HOLD", "reason_code": "CANT_TRADE"})
        return action_plan

    current_position = position_context.get('current_position', {}) if position_context else {}
    if current_position.get('has_position') and current_position.get('size', 0) != 0:
        action_plan.update({"action": "HOLD", "reason_code": "HAS_POSITION"})
        return action_plan

    slope_15 = slopes.get('15m')
    slope_30 = slopes.get('30m')

    if slope_15 is None or slope_30 is None:
        action_plan.update({"action": "HOLD", "reason_code": "NO_DATA"})
        return action_plan

    if abs(slope_15) < SLOPE_THRESHOLD and abs(slope_30) < SLOPE_THRESHOLD:
        action_plan.update({"action": "HOLD", "reason_code": "RANGE"})
        return action_plan

    if slope_15 > SLOPE_THRESHOLD and slope_30 > SLOPE_THRESHOLD:
        action_plan.update({"action": "BUY", "reason_code": "TREND_UP"})
        return action_plan

    if slope_15 < -SLOPE_THRESHOLD and slope_30 < -SLOPE_THRESHOLD:
        action_plan.update({"action": "SELL", "reason_code": "TREND_DOWN"})
        return action_plan

    action_plan.update({"action": "HOLD", "reason_code": "MIXED"})
    return action_plan
