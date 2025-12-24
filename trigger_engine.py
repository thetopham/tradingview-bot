import logging
from collections import Counter
from datetime import datetime
from typing import Dict

from auth import in_get_flat
from config import load_config

logger = logging.getLogger(__name__)
config = load_config()
CT = config['CT']
SLOPE_THRESHOLD = 0.00012


def _direction_from_slope(value: float) -> str:
    if value is None:
        return "unknown"
    if abs(value) < SLOPE_THRESHOLD:
        return "flat"
    return "up" if value > 0 else "down"


def decide(market_state: Dict, position_context: Dict) -> Dict:
    """Return an ActionPlan dict with HOLD/BUT/SELL and reason_code."""
    now = datetime.now(CT)
    if in_get_flat(now):
        return {"action": "HOLD", "reason_code": "in_get_flat_window", "details": {"ts": now.isoformat()}}

    account_metrics = position_context.get("account_metrics", {}) if position_context else {}
    if not account_metrics.get("can_trade", False):
        return {"action": "HOLD", "reason_code": "account_cannot_trade", "details": account_metrics}

    current_position = (position_context or {}).get("current_position", {})
    if current_position.get("has_position"):
        return {"action": "HOLD", "reason_code": "position_open", "details": current_position}

    tf_directions = {}
    for tf, tf_state in (market_state or {}).get("timeframes", {}).items():
        tf_directions[tf] = _direction_from_slope(tf_state.get("normalized_slope"))

    if not tf_directions:
        return {"action": "HOLD", "reason_code": "no_market_state"}

    counts = Counter(tf_directions.values())
    trending = {k: v for k, v in counts.items() if k in ("up", "down")}

    if not trending:
        return {"action": "HOLD", "reason_code": "flat_or_insufficient", "details": tf_directions}

    if len(trending) > 1:
        return {"action": "HOLD", "reason_code": "mixed_regime", "details": tf_directions}

    direction = next(iter(trending))
    action = "BUY" if direction == "up" else "SELL"
    return {"action": action, "reason_code": f"trend_{direction}", "details": tf_directions}

