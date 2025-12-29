"""Pure helpers for position management decisions."""

from typing import Dict, Tuple


def decide_pm_action(
    position_state: Dict,
    market_signal: str,
    account_state: Dict,
    opp_count: int,
    cfg: Dict,
) -> Tuple[str, str]:
    """Return (action, reason_code) for position management decisions."""

    can_trade = account_state.get("can_trade", True)
    if not can_trade:
        return "FLAT", "RISK_CAN_TRADE_FALSE"

    current_pnl = float(position_state.get("current_pnl") or 0)
    duration = float(position_state.get("duration_minutes") or 0)
    side = position_state.get("side")
    signal = (market_signal or "HOLD").upper()

    cut_loss = float(cfg.get("PM_CUT_LOSS", -20.0))
    opp_threshold = int(cfg.get("PM_OPPOSITE_PERSIST_K", 2))
    opp_min_pnl = float(cfg.get("PM_OPPOSITE_MIN_PNL", -5.0))
    time_stop_minutes = float(cfg.get("PM_TIME_STOP_MINUTES", 20))
    time_stop_band = float(cfg.get("PM_TIME_STOP_PNL_BAND", 5.0))

    opposite = cfg.get("opposite")
    if opposite is None:
        opposite = (side == "LONG" and signal == "SELL") or (side == "SHORT" and signal == "BUY")

    if current_pnl <= cut_loss:
        return "FLAT", "CUT_LOSER"

    if opposite and opp_count >= opp_threshold and current_pnl <= opp_min_pnl:
        return "FLAT", "OPP_PERSIST"

    if duration >= time_stop_minutes and abs(current_pnl) <= time_stop_band:
        return "FLAT", "TIME_STOP"

    return "HOLD", "HOLD"
