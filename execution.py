import logging
from typing import Dict

from api import get_contract, place_market


def send_entry(action: str, acct_id: int, symbol: str, size: int, trading_enabled: bool) -> Dict:
    if action not in ("BUY", "SELL"):
        return {"sent": False, "simulated": True, "reason": "non_entry_action"}

    contract_id = get_contract(symbol)
    if not contract_id:
        logging.error("No contract found for symbol %s", symbol)
        return {"sent": False, "simulated": True, "reason": "missing_contract"}

    if not trading_enabled:
        logging.info("SIMULATED ORDER: %s %s %s (trading disabled)", action, size, symbol)
        return {"sent": False, "simulated": True, "reason": "trading_disabled", "contract_id": contract_id}

    side = 0 if action == "BUY" else 1
    response = place_market(acct_id, contract_id, side, size)
    return {"sent": True, "simulated": False, "response": response, "contract_id": contract_id}
