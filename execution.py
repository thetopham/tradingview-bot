import logging
from typing import Dict

from api import get_contract, place_market

logger = logging.getLogger(__name__)

SIDE_MAP = {
    "BUY": 0,
    "SELL": 1,
}


def send_entry(action: str, acct_id: int, symbol: str, size: int, trading_enabled: bool) -> Dict:
    if action not in ("BUY", "SELL"):
        return {"sent": False, "simulated": True, "reason": "non_entry_action"}

    if not trading_enabled:
        logger.info("SIMULATED ORDER: %s %s %s (trading disabled)", action, size, symbol)
        return {"sent": False, "simulated": True, "action": action, "size": size, "symbol": symbol}

    cid = get_contract(symbol)
    side = SIDE_MAP.get(action)
    response = place_market(acct_id, cid, side, size)
    logger.info("Sent market order: %s %s %s response=%s", action, size, symbol, response)
    return {"sent": True, "simulated": False, "response": response, "action": action, "symbol": symbol, "size": size}
