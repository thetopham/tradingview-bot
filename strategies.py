# strategies.py

"""Simplified strategy execution focused on single-call server-side bracket orders."""

import logging
from datetime import datetime

from api import (
    get_contract,
    search_pos,
    flatten_contract,
    place_bracket_order,
)
from config import load_config
from signalr_listener import track_trade

config = load_config()
CT = config['CT']
BRACKET_TEMPLATES = config.get('BRACKET_TEMPLATES', {})
DEFAULT_TEMPLATE = config.get('DEFAULT_BRACKET_TEMPLATE', 'standard')


def _select_template(account_name: str | None, override: str | None = None) -> str:
    if override:
        return override
    if account_name:
        template = BRACKET_TEMPLATES.get(account_name.lower())
        if template:
            return template
    return DEFAULT_TEMPLATE


def run_simple_bracket(
    acct_id: int,
    account_name: str,
    sym: str,
    direction: str,
    size: int,
    alert: str,
    ai_decision_id=None,
    template: str | None = None,
    regime: str = "unknown",
):
    """
    Submit a single server-side bracket order without local SL/TP management.
    direction: BUY or SELL.
    """
    cid = get_contract(sym)
    if not cid:
        logging.error("run_simple_bracket: unable to resolve contract for %s", sym)
        return

    side = 0 if direction.upper() == "BUY" else 1
    positions = [p for p in search_pos(acct_id) if p.get("contractId") == cid]

    if any((side == 0 and p.get("type") == 1) or (side == 1 and p.get("type") == 2) for p in positions):
        logging.info("Existing position already matches %s; skipping new bracket", direction)
        return

    if any((side == 0 and p.get("type") == 2) or (side == 1 and p.get("type") == 1) for p in positions):
        logging.info("Flattening opposing position before submitting new bracket")
        if not flatten_contract(acct_id, cid, timeout=10):
            logging.error("Flatten failed; aborting bracket entry")
            return

    template_name = _select_template(account_name, template)
    order = place_bracket_order(acct_id, cid, side, size, template_name)
    order_id = order.get("orderId") if isinstance(order, dict) else None

    track_trade(
        acct_id=acct_id,
        cid=cid,
        entry_time=datetime.now(CT).timestamp(),
        ai_decision_id=ai_decision_id,
        strategy="simple_bracket",
        sig=direction,
        size=size,
        order_id=order_id,
        alert=alert,
        account=acct_id,
        symbol=sym,
        sl_id=None,
        tp_ids=None,
        trades=None,
        regime=regime,
    )

    logging.info(
        "Submitted bracket order %s for %s: %s %s using template %s",
        order_id,
        sym,
        direction,
        size,
        template_name,
    )
