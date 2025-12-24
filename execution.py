import logging
from typing import Optional

from api import get_contract, place_market
from config import load_config

config = load_config()
DEFAULT_SIZE = int(config.get("DEFAULT_TRADE_SIZE", 1))


def send_entry(action: str, acct_id: int, symbol: str, size: Optional[int] = None):
    action_upper = action.upper()
    if action_upper not in {"BUY", "SELL"}:
        logging.warning("send_entry called with non-trade action: %s", action)
        return None

    cid = get_contract(symbol)
    if not cid:
        logging.error("Unable to resolve contract for symbol %s", symbol)
        return None

    trade_size = size or DEFAULT_SIZE
    side = 0 if action_upper == "BUY" else 1
    logging.info(
        "Placing market order: action=%s acct=%s symbol=%s size=%s", action_upper, acct_id, symbol, trade_size
    )
    response = place_market(acct_id, cid, side, trade_size)
    logging.info("Order response: %s", response)
    return response
