import logging
from typing import Dict

from api import get_contract, place_market

logger = logging.getLogger(__name__)

SIDE_MAP = {
    "BUY": 0,
    "SELL": 1,
}


def send_entry(action: str, acct_id: int, symbol: str, size: int, trading_enabled: bool) -> Dict:
    if action not in SIDE_MAP:
        return {"sent": False, "simulated": False, "reason": "invalid_action"}

    if not trading_enabled:
        logger.info("SIMULATED ORDER: %s %s x%s (TRADING_ENABLED=false)", action, symbol, size)
        return {"sent": False, "simulated": True, "reason": "trading_disabled"}

    try:
        contract_id = get_contract(symbol)
    except Exception as exc:
        logger.error("Failed to resolve contract for %s: %s", symbol, exc)
        return {"sent": False, "simulated": False, "reason": "contract_lookup_failed"}

    try:
        side = SIDE_MAP[action]
        response = place_market(acct_id, contract_id, side, size)
        return {"sent": True, "simulated": False, "response": response}
    except Exception as exc:
        logger.error("Error sending market order: %s", exc)
        return {"sent": False, "simulated": False, "reason": str(exc)}
