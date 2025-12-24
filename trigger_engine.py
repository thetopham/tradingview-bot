import logging
from dataclasses import dataclass
from typing import Dict

from auth import in_get_flat
from config import load_config
from market_state import MarketState

config = load_config()
CT = config.get("CT")
NORMALIZED_SLOPE_THRESHOLD = 0.0001


@dataclass
class ActionPlan:
    action: str
    reason_code: str


def decide(market_state: MarketState, position_context: Dict) -> ActionPlan:
    now = market_state.as_of

    if in_get_flat(now):
        return ActionPlan(action="HOLD", reason_code="get_flat_window")

    current_position = position_context.get("current_position", {}) if position_context else {}
    account_metrics = position_context.get("account_metrics", {}) if position_context else {}

    if current_position.get("has_position"):
        return ActionPlan(action="HOLD", reason_code="position_open")

    if not account_metrics.get("can_trade", False):
        return ActionPlan(action="HOLD", reason_code="risk_gate_blocked")

    if not market_state.timeframes:
        return ActionPlan(action="HOLD", reason_code="no_market_state")

    slopes = [
        tf_state.normalized_slope
        for tf_state in market_state.timeframes.values()
        if tf_state.normalized_slope is not None
    ]

    if not slopes:
        return ActionPlan(action="HOLD", reason_code="no_slopes")

    if any(abs(slope) < NORMALIZED_SLOPE_THRESHOLD for slope in slopes):
        return ActionPlan(action="HOLD", reason_code="flat_slope")

    positive = [s for s in slopes if s > 0]
    negative = [s for s in slopes if s < 0]

    if positive and negative:
        return ActionPlan(action="HOLD", reason_code="mixed_regime")

    if positive and not negative:
        return ActionPlan(action="BUY", reason_code="trend_up")

    if negative and not positive:
        return ActionPlan(action="SELL", reason_code="trend_down")

    return ActionPlan(action="HOLD", reason_code="fallback")
