# strategies.py

import logging
from datetime import datetime
from typing import Optional

from api import (
    get_contract,
    search_pos,
    place_market,
    place_bracket_order,
)
from signalr_listener import track_trade
from config import load_config

config = load_config()
CT = config['CT']
BRACKET_TEMPLATE_DEFAULT = config.get('BRACKET_TEMPLATE_DEFAULT', '')
BRACKET_TEMPLATES = config.get('BRACKET_TEMPLATES', {})

ALLOWED_ACTIONS = {"BUY", "SELL"}
ALLOWED_INTENTS = {"ENTER", "EXIT", "ADD", "REDUCE", "HOLD", "FLAT", None}


def _select_bracket_template(account: str, explicit_template: Optional[str] = None) -> str:
    if explicit_template:
        return explicit_template
    if account:
        tmpl = BRACKET_TEMPLATES.get(account.lower())
        if tmpl:
            return tmpl
    return BRACKET_TEMPLATE_DEFAULT


def _compute_net_position(positions: list[dict], cid: str) -> int:
    relevant = [p for p in positions if p.get("contractId") == cid]
    return sum(p.get("size", 0) if p.get("type") == 1 else -p.get("size", 0) for p in relevant)


def execute_bracket_strategy(
    acct_name: str,
    acct_id: int,
    sym: str,
    action: str,
    intent: Optional[str],
    size: int,
    alert: str = "",
    ai_decision_id: Optional[str | int] = None,
    bracket_template: Optional[str] = None,
    time_in_force: str = "DAY",
):
    """
    Submit a single server-side bracket order based on a simplified AI directive.

    The caller should already have validated `action` and `intent`. This function
    performs a minimal position check and avoids polling or SL/TP micromanagement.
    """
    action = (action or "").upper()
    intent = (intent or "").upper() if intent else None

    if action not in ALLOWED_ACTIONS:
        logging.info("execute_bracket_strategy: unsupported action %s", action)
        return
    if intent not in ALLOWED_INTENTS:
        logging.info("execute_bracket_strategy: unsupported intent %s", intent)
        return
    if size <= 0:
        logging.info("execute_bracket_strategy: non-positive size %s", size)
        return

    cid = get_contract(sym)
    if not cid:
        logging.error("Could not resolve contract for symbol %s", sym)
        return

    positions = search_pos(acct_id) or []
    net_pos = _compute_net_position(positions, cid)

    # Handle exit/reduce paths first to avoid over-trading
    if intent in {"EXIT", "FLAT"} and net_pos:
        close_side = 1 if net_pos > 0 else 0
        logging.info(
            "Closing existing position before FLAT/EXIT: acct=%s cid=%s size=%s side=%s",
            acct_id, cid, abs(net_pos), close_side
        )
        place_market(acct_id, cid, close_side, abs(net_pos))
        return

    if intent == "REDUCE" and net_pos:
        close_side = 1 if net_pos > 0 else 0
        reduce_size = min(abs(net_pos), size)
        if reduce_size > 0:
            logging.info(
                "Reducing position: acct=%s cid=%s reduce=%s side=%s",
                acct_id, cid, reduce_size, close_side
            )
            place_market(acct_id, cid, close_side, reduce_size)
            return

    # If existing position opposes requested action, flatten then proceed
    if net_pos:
        if (net_pos > 0 and action == "SELL") or (net_pos < 0 and action == "BUY"):
            close_side = 1 if net_pos > 0 else 0
            logging.info(
                "Flattening opposing position before entry: acct=%s cid=%s size=%s side=%s",
                acct_id, cid, abs(net_pos), close_side
            )
            place_market(acct_id, cid, close_side, abs(net_pos))
            net_pos = 0

    template = _select_bracket_template(acct_name, bracket_template)
    if not template:
        logging.error("No bracket template available for account %s", acct_name)
        return

    side = 0 if action == "BUY" else 1
    order = place_bracket_order(acct_id, cid, side, size, template, time_in_force=time_in_force)

    entry_time = datetime.now(CT)
    track_trade(
        acct_id=acct_id,
        cid=cid,
        entry_time=entry_time.timestamp(),
        ai_decision_id=ai_decision_id,
        strategy="server_bracket",
        sig=action,
        size=size,
        order_id=order.get("orderId") if isinstance(order, dict) else None,
        alert=alert,
        account=acct_id,
        symbol=sym,
        sl_id=None,
        tp_ids=[],
        trades=None,
        regime="unknown"
    )
