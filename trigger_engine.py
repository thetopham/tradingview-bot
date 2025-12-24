import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from auth import in_get_flat
from config import load_config

config = load_config()
CT = config["CT"]
DEFAULT_SLOPE_THRESHOLD = 0.00002


@dataclass
class ActionPlan:
    action: str
    reason_code: str
    details: Optional[Dict] = None


TREND_ACTIONS = {"up": "BUY", "down": "SELL"}


def _gated_hold(reason: str, details: Optional[Dict] = None) -> ActionPlan:
    return ActionPlan(action="HOLD", reason_code=reason, details=details or {})


def decide(market_state, position_context: Optional[Dict], slope_threshold: float = DEFAULT_SLOPE_THRESHOLD,
           now: Optional[datetime] = None) -> ActionPlan:
    now = now or datetime.now(CT)

    if market_state is None:
        return _gated_hold("missing_market_state")

    if position_context is None:
        return _gated_hold("missing_position_context")

    if in_get_flat(now):
        return _gated_hold("get_flat_window")

    acct_metrics = position_context.get("account_metrics", {})
    if acct_metrics and not acct_metrics.get("can_trade", True):
        return _gated_hold("account_cannot_trade", {"account_metrics": acct_metrics})

    current_position = position_context.get("current_position", {})
    if current_position.get("has_position"):
        return _gated_hold("existing_position", {"current_position": current_position})

    slopes: Dict[str, float] = {}
    for tf, state in market_state.timeframes.items():
        slope_val = getattr(state, "normalized_slope", None)
        if slope_val is not None:
            slopes[tf] = slope_val

    if not slopes:
        return _gated_hold("no_slope_data")

    trend_votes = []
    for tf, slope in slopes.items():
        if abs(slope) < slope_threshold:
            trend_votes.append("range")
        else:
            trend_votes.append("up" if slope > 0 else "down")

    if all(vote == "range" for vote in trend_votes):
        return _gated_hold("range_below_threshold", {"slopes": slopes})

    up_votes = trend_votes.count("up")
    down_votes = trend_votes.count("down")

    if up_votes and down_votes:
        return _gated_hold("mixed_trend", {"slopes": slopes})

    trend = "up" if up_votes else "down"
    action = TREND_ACTIONS.get(trend, "HOLD")
    return ActionPlan(action=action, reason_code=f"trend_{trend}", details={"slopes": slopes})
