import logging

from api import get_contract, place_market

logger = logging.getLogger(__name__)

SIDE_MAP = {
    "BUY": 0,
    "SELL": 1,
}


def send_entry(action: str, acct_id: int, symbol: str, size: int, trading_enabled: bool) -> dict:
    action = action.upper()
    if action not in SIDE_MAP:
        return {"sent": False, "simulated": False, "reason": "NO_ACTION"}

    if not trading_enabled:
        logger.info(
            "SIMULATED ORDER: %s size=%s symbol=%s trading_enabled=%s",
            action,
            size,
            symbol,
            trading_enabled,
        )
        return {"sent": False, "simulated": True, "reason": "TRADING_DISABLED"}

    try:
        cid = get_contract(symbol)
    except Exception as exc:
        logger.error("Failed to resolve contract for %s: %s", symbol, exc)
        return {"sent": False, "simulated": False, "reason": "NO_CONTRACT"}

    side = SIDE_MAP[action]
    logger.info("Sending market order: acct=%s cid=%s side=%s size=%s", acct_id, cid, side, size)
    response = place_market(acct_id, cid, side, size)
    return {"sent": True, "simulated": False, "response": response}

