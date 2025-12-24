"""Execution helpers for reduction trigger architecture."""

from __future__ import annotations

import logging
from typing import Optional

from api import get_active_contract_for_symbol_cached, place_market
from config import load_config

logger = logging.getLogger(__name__)

CONFIG = load_config()
DEFAULT_TRADE_SIZE = int(CONFIG.get("DEFAULT_TRADE_SIZE", 1))
TRADING_ENABLED = CONFIG.get("TRADING_ENABLED", False)


def send_entry(action: str, acct_id: int, symbol: str, size: Optional[int] = None) -> None:
    if action not in {"BUY", "SELL"}:
        logger.info("send_entry called with non-entry action=%s; skipping", action)
        return

    trade_size = size or DEFAULT_TRADE_SIZE
    side = 0 if action == "BUY" else 1
    contract_id = get_active_contract_for_symbol_cached(symbol)
    if not contract_id:
        logger.error("No contract ID resolved for %s; cannot place order", symbol)
        return

    logger.info(
        "Preparing market entry action=%s symbol=%s contract=%s size=%s trading_enabled=%s",
        action,
        symbol,
        contract_id,
        trade_size,
        TRADING_ENABLED,
    )

    if not TRADING_ENABLED:
        logger.info("TRADING_ENABLED is false; skipping live order placement")
        return

    place_market(acct_id, contract_id, side, trade_size)
    logger.info("Submitted market order acct=%s action=%s size=%s", acct_id, action, trade_size)


__all__ = ["send_entry", "TRADING_ENABLED", "DEFAULT_TRADE_SIZE"]
