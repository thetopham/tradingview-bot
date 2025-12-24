import logging

from api import get_contract, place_market
from config import load_config

config = load_config()
DEFAULT_TRADE_SIZE = int(config.get("DEFAULT_TRADE_SIZE", 1))


def send_entry(action: str, acct_id: int, symbol: str, size: int = DEFAULT_TRADE_SIZE):
    action = (action or "").upper()
    if action not in ("BUY", "SELL"):
        logging.info("Execution skipped for action=%s", action)
        return None

    try:
        contract_id = get_contract(symbol)
    except Exception as exc:
        logging.error("Could not resolve contract for %s: %s", symbol, exc)
        return None

    side = 0 if action == "BUY" else 1
    logging.info("Placing market %s for %s size=%s (acct=%s)", action, symbol, size, acct_id)
    return place_market(acct_id, contract_id, side, size)
