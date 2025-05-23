#!/usr/bin/env python3
# tradingview_projectx_bot.py

"""
Main entry point for ProjectX Trading Bot.
Handles webhooks, AI decisions, trade execution, and scheduled processing.
"""

from flask import Flask, request, jsonify
from logging_config import setup_logging
from config import load_config
from api import (
    flatten_contract, get_contract, ai_trade_decision, cancel_all_stops, search_pos 
    )
from strategies import run_bracket, run_brackmod, run_pivot
from scheduler import start_scheduler
from auth import in_get_flat, authenticate, get_token, get_token_expiry, ensure_token
from signalr_listener import launch_signalr_listener

import threading
from datetime import datetime

# --- Logging/Config/Globals ---
setup_logging()
config = load_config()

TV_PORT         = config['TV_PORT']
WEBHOOK_SECRET  = config['WEBHOOK_SECRET']
ACCOUNTS        = config['ACCOUNTS']
DEFAULT_ACCOUNT = config['DEFAULT_ACCOUNT']
CT              = config['CT']
GET_FLAT_START  = config['GET_FLAT_START']
GET_FLAT_END    = config['GET_FLAT_END']

AI_ENDPOINTS = {
    "epsilon": config['N8N_AI_URL'],
    "beta": config['N8N_AI_URL2'],
}

AUTH_LOCK = threading.Lock()

app = Flask(__name__)

# --- Health Check Route (optional, but recommended for uptime monitoring) ---
@app.route("/healthz")
def healthz():
    return jsonify(status="ok", time=str(datetime.now(CT)))

# --- Webhook Trading Route ---
@app.route("/webhook", methods=["POST"])
def tv_webhook():
    data = request.get_json()
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="unauthorized"), 403

    strat = data.get("strategy", "bracket").lower()
    acct  = (data.get("account") or DEFAULT_ACCOUNT).lower()
    sig   = data.get("signal", "").upper()
    sym   = data.get("symbol", "")
    size  = int(data.get("size", 1))
    alert = data.get("alert", "")
    ai_decision_id = data.get("ai_decision_id", None)

    if acct not in ACCOUNTS:
        return jsonify(error=f"Unknown account '{acct}'"), 400

    acct_id = ACCOUNTS[acct]
    cid = get_contract(sym)

    # Manual flatten (close all) signal
    if sig == "FLAT":
        ok = flatten_contract(acct_id, cid, timeout=10)
        status = "ok" if ok else "error"
        code = 200 if ok else 500
        return jsonify(status=status, strategy=strat, message="flattened"), code

    now = datetime.now(CT)
    if in_get_flat(now):
        return jsonify(status="ok", strategy=strat, message="in get-flat window, no trades"), 200

    # --- AI Overseer Routing ---
    if acct in AI_ENDPOINTS:
        ai_url = AI_ENDPOINTS[acct]
        ai_decision = ai_trade_decision(acct, strat, sig, sym, size, alert, ai_url)
        if ai_decision.get("signal", "").upper() not in ("BUY", "SELL"):
            return jsonify(status="blocked", reason=ai_decision.get("reason", "No reason"), ai_decision=ai_decision), 200
        # Overwrite user values with AI's preferred decision
        strat = ai_decision.get("strategy", strat)
        sig = ai_decision.get("signal", sig)
        sym = ai_decision.get("symbol", sym)
        size = ai_decision.get("size", size)
        alert = ai_decision.get("alert", alert)
        ai_decision_id = ai_decision.get("ai_decision_id", ai_decision_id)

    # Cancel all stops before every new entry (safety)
    positions = search_pos(acct_id)
    open_pos = [p for p in positions if p["contractId"] == cid and p.get("size", 0) != 0]
    if not open_pos:
        cancel_all_stops(acct_id, cid)


    # --- Strategy Dispatch ---
    if strat == "bracket":
        return run_bracket(acct_id, sym, sig, size, alert, ai_decision_id)
    elif strat == "brackmod":
        return run_brackmod(acct_id, sym, sig, size, alert, ai_decision_id)
    elif strat == "pivot":
        return run_pivot(acct_id, sym, sig, size, alert, ai_decision_id)
    else:
        return jsonify(error=f"Unknown strategy '{strat}'"), 400

if __name__ == "__main__":
    try:
        authenticate()
        signalr_listener = launch_signalr_listener(
            get_token=get_token,
            get_token_expiry=get_token_expiry,
            authenticate=authenticate,
            auth_lock=AUTH_LOCK
        ) 
        scheduler = start_scheduler(app)
        app.logger.info("Starting server.")
        app.run(host="0.0.0.0", port=TV_PORT, threaded=True)
    except Exception as e:
        import logging
        logging.exception(f"Fatal error during startup: {e}")
