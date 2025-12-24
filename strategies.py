# strategies.py

"""Simplified bracket execution helpers (server-side brackets, no client SL/TP)."""

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


def _default_bracket(account: str) -> str:
    return config.get('ACCOUNT_BRACKETS', {}).get(
        account.lower(), config.get('BRACKET_TEMPLATE', 'default')
    )


def _net_position(positions: list) -> int:
    return sum(p.get("size", 0) if p.get("type") == 1 else -p.get("size", 0) for p in positions)


def execute_bracket_decision(acct_id: int, account_name: str, symbol: str, decision: dict,
                              alert: str = "", ai_decision_id=None):
    """
    Execute a high-level AI decision using a single server-side bracket order.

    decision keys: action (enter/add/reduce/exit/hold), direction (BUY/SELL/FLAT),
    size (int), bracket (template name).
    """
    action = (decision.get('action') or 'hold').lower()
    direction = (decision.get('direction') or decision.get('signal') or '').upper()
    bracket_template = decision.get('bracket') or _default_bracket(account_name)
    size = int(decision.get('size') or 0)

    if action == 'hold' or direction == 'HOLD':
        logging.info("Decision is HOLD; no action taken.")
        return

    cid = get_contract(symbol)
    if not cid:
        logging.error("Could not resolve contract for %s; skipping order", symbol)
        return

    positions = [p for p in search_pos(acct_id) if p.get("contractId") == cid and p.get("size", 0) > 0]
    net_pos = _net_position(positions)

    if action in ('exit', 'reduce') or direction == 'FLAT':
        if net_pos:
            logging.info("Flattening position for %s (%s contracts)", symbol, net_pos)
            flatten_contract(acct_id, cid)
            track_trade(
                acct_id=acct_id,
                cid=cid,
                entry_time=datetime.now(CT).timestamp(),
                ai_decision_id=ai_decision_id,
                strategy="bracket_exit",
                sig='SELL' if net_pos > 0 else 'BUY',
                size=abs(net_pos),
                order_id=None,
                alert=alert,
                account=acct_id,
                symbol=symbol,
                sl_id=None,
                tp_ids=None,
                trades=None,
                regime=decision.get('regime', 'unknown')
            )
        else:
            logging.info("Exit decision received while flat; nothing to do.")
        return

    if direction not in ('BUY', 'SELL'):
        logging.warning("Unsupported direction %s; skipping order", direction)
        return

    side = 0 if direction == 'BUY' else 1

    if net_pos and ((net_pos > 0 and side == 1) or (net_pos < 0 and side == 0)):
        logging.info("Opposite position detected (%s); flattening before entry", net_pos)
        flatten_contract(acct_id, cid)
        net_pos = 0

    order_size = size if size > 0 else 1

    order = place_bracket_order(acct_id, cid, side, order_size, bracket_template)
    order_id = order.get("orderId") if isinstance(order, dict) else None

    track_trade(
        acct_id=acct_id,
        cid=cid,
        entry_time=datetime.now(CT).timestamp(),
        ai_decision_id=ai_decision_id,
        strategy="bracket_single",
        sig=direction,
        size=order_size,
        order_id=order_id,
        alert=alert,
        account=acct_id,
        symbol=symbol,
        sl_id=None,
        tp_ids=None,
        trades=None,
        regime=decision.get('regime', 'unknown'),
    )
