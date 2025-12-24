import logging
from typing import Dict

from api import get_contract, place_market
from config import load_config

config = load_config()
TRADING_ENABLED = config.get("TRADING_ENABLED", False)
DEFAULT_SIZE = config.get("DEFAULT_SIZE", 1)

logger = logging.getLogger(__name__)


def send_entry(action: str, acct_id: int, symbol: str, size: int = DEFAULT_SIZE) -> Dict:
    """Submit a single market entry. Returns broker response or skip reason."""
    if action not in {"BUY", "SELL"}:
        return {"status": "skipped", "reason": f"no_entry_for_{action}"}

    side = 0 if action == "BUY" else 1
    cid = get_contract(symbol)

    if not TRADING_ENABLED:
        logger.info("TRADING_ENABLED=false -> would %s %s %s", action, size, symbol)
        return {"status": "dry_run", "action": action, "symbol": symbol, "size": size, "contract": cid}

    response = place_market(acct_id, cid, side, size)
    logger.info("Submitted market order -> %s", response)
    return {"status": "sent", "response": response, "symbol": symbol, "size": size}

