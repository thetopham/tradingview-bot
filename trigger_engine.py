"""Decision engine for reduction trade trigger."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Optional

from auth import in_get_flat
from config import load_config
from market_state import NORMALIZED_SLOPE_THRESHOLD

logger = logging.getLogger(__name__)

CONFIG = load_config()
CT = CONFIG["CT"]
TREND_TIMEFRAMES = ("5m", "15m", "30m")


class ActionPlan(Dict):
    action: str
    reason_code: str
    details: Dict


def _classify_timeframes(market_state: Dict) -> Dict[str, str]:
    regimes = {}
    for tf, info in market_state.get("timeframes", {}).items():
        regimes[tf] = info.get("regime")
    return regimes


def _has_conflict(regimes: Dict[str, str]) -> bool:
    directions = {r for r in regimes.values() if r in ("trend_up", "trend_down")}
    return len(directions) > 1


def _missing_data(regimes: Dict[str, str]) -> bool:
    return any(regimes.get(tf) in (None, "insufficient") for tf in TREND_TIMEFRAMES)


def decide(market_state: Optional[Dict], position_context: Optional[Dict], now: Optional[datetime] = None) -> ActionPlan:
    now = now or datetime.now(CT)
    plan: ActionPlan = {
        "action": "HOLD",
        "reason_code": "uninitialized",
        "details": {},
    }

    if market_state is None:
        plan["reason_code"] = "no_market_state"
        return plan

    plan["details"]["market_errors"] = market_state.get("errors", [])
    regimes = _classify_timeframes(market_state)
    plan["details"]["regimes"] = regimes
    slopes = {
        tf: market_state["timeframes"].get(tf, {}).get("normalized_slope")
        for tf in TREND_TIMEFRAMES
        if tf in market_state.get("timeframes", {})
    }
    plan["details"]["slopes"] = slopes

    if in_get_flat(now):
        plan["reason_code"] = "get_flat_window"
        return plan

    if position_context is None:
        plan["reason_code"] = "no_position_context"
        return plan

    account_metrics = position_context.get("account_metrics", {})
    current_position = position_context.get("current_position", {})

    if current_position.get("has_position"):
        plan["reason_code"] = "position_open"
        return plan

    if not account_metrics.get("can_trade", False):
        plan["reason_code"] = "account_gate_block"
        return plan

    if _missing_data(regimes):
        plan["reason_code"] = "insufficient_data"
        return plan

    if any(abs((slopes.get(tf) or 0)) < NORMALIZED_SLOPE_THRESHOLD for tf in TREND_TIMEFRAMES if tf in slopes):
        plan["reason_code"] = "range"
        return plan

    if _has_conflict(regimes):
        plan["reason_code"] = "mixed_trend"
        return plan

    first_trend = next((regimes[tf] for tf in TREND_TIMEFRAMES if regimes.get(tf) in ("trend_up", "trend_down")), None)
    if first_trend == "trend_up":
        plan["action"] = "BUY"
        plan["reason_code"] = "aligned_uptrend"
    elif first_trend == "trend_down":
        plan["action"] = "SELL"
        plan["reason_code"] = "aligned_downtrend"
    else:
        plan["reason_code"] = "no_trend_detected"

    logger.info("Action plan: action=%s reason=%s slopes=%s", plan["action"], plan["reason_code"], slopes)
    return plan


__all__ = ["decide", "ActionPlan"]
