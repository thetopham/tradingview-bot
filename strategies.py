# strategies.py

"""Lean strategy dispatch built around server-side bracket templates."""

import logging
from datetime import datetime
from typing import Optional

from api import (
    get_contract,
    search_pos,
    flatten_contract,
    place_bracket_order,
)
from signalr_listener import track_trade
from config import load_config
from api import get_market_conditions_summary

config = load_config()
CT = config['CT']
BRACKET_TEMPLATE_DEFAULT = config.get('BRACKET_TEMPLATE_DEFAULT', 'default')
BRACKET_TEMPLATE_MAP = config.get('BRACKET_TEMPLATE_MAP', {})


def resolve_bracket_template(account_name: Optional[str]) -> str:
    """Pick the bracket template for an account, falling back to the default."""
    if account_name:
        return BRACKET_TEMPLATE_MAP.get(account_name.lower(), BRACKET_TEMPLATE_DEFAULT)
    return BRACKET_TEMPLATE_DEFAULT


def execute_bracket(acct_id: int, sym: str, sig: str, size: int, alert: str,
                    ai_decision_id: Optional[str] = None, account_name: Optional[str] = None,
                    time_in_force: Optional[str] = None):
    """
    Submit a single server-side bracket order using the configured template.
    Avoids client-side TP/SL micromanagement and minimizes polling.
    """
    cid = get_contract(sym)
    if not cid:
        logging.error("execute_bracket: No contract ID for symbol %s", sym)
        return

    side = 0 if sig == "BUY" else 1

    positions = [p for p in (search_pos(acct_id) or []) if p.get("contractId") == cid]

    # skip if same-direction position already exists
    if any((side == 0 and p.get("type") == 1) or (side == 1 and p.get("type") == 2) for p in positions):
        logging.info("execute_bracket: position already open in same direction; skipping")
        return

    # flatten if opposite position exists
    if any((side == 0 and p.get("type") == 2) or (side == 1 and p.get("type") == 1) for p in positions):
        if not flatten_contract(acct_id, cid, timeout=10):
            logging.error("execute_bracket: could not flatten opposite exposure; aborting")
            return

    template = resolve_bracket_template(account_name)
    custom_tag = f"ai:{ai_decision_id}" if ai_decision_id else None

    try:
        regime_data = get_market_conditions_summary()
    except Exception:
        regime_data = None

    entry_time = datetime.now(CT)
    order = place_bracket_order(
        acct_id=acct_id,
        cid=cid,
        side=side,
        size=size,
        template=template,
        time_in_force=time_in_force,
        custom_tag=custom_tag,
    )
    order_id = order.get("orderId")

    track_trade(
        acct_id=acct_id,
        cid=cid,
        entry_time=entry_time.timestamp(),
        ai_decision_id=ai_decision_id,
        strategy="bracket_template",
        sig=sig,
        size=size,
        order_id=order_id,
        alert=alert,
        account=acct_id,
        symbol=sym,
        sl_id=None,
        tp_ids=None,
        trades=None,
        regime=regime_data.get('regime', 'unknown') if regime_data else 'unknown'
    )
