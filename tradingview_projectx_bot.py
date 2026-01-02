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
    flatten_contract, get_contract, ai_trade_decision, search_pos
    )
from position_manager import PositionManager
from strategies import run_simple
from scheduler import start_scheduler
from auth import in_get_flat, authenticate, get_token, get_token_expiry, ensure_token
from signalr_listener import launch_signalr_listener
from dashboard import dashboard_bp
from threading import Thread
import threading
from datetime import datetime
import logging

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
    "beta": config['N8N_AI_URL'],
}

AUTH_LOCK = threading.Lock()
POSITION_MANAGER = PositionManager(ACCOUNTS)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.register_blueprint(dashboard_bp)

# --- Health Check Route (optional, but recommended for uptime monitoring) ---
@app.route("/healthz")
def healthz():
    return jsonify(status="ok", time=str(datetime.now(CT)))

@app.route("/webhook", methods=["POST"])
def tv_webhook():
    data = request.get_json()
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="unauthorized"), 403

    # Respond immediately to TradingView/n8n
    Thread(target=handle_webhook_logic, args=(data,)).start()
    return jsonify(status="accepted", msg="Processing started"), 202

def handle_webhook_logic(data):
    try:
        strat = (data.get("strategy") or "simple").lower()
        acct  = (data.get("account") or DEFAULT_ACCOUNT).lower()
        sig   = data.get("signal", "").upper()
        sym   = data.get("symbol", "")
        size  = int(data.get("size", 1))
        alert = data.get("alert", "")
        ai_decision_id = data.get("ai_decision_id", None)

        if acct not in ACCOUNTS:
            logging.error(f"Unknown account '{acct}'")
            return

        acct_id = ACCOUNTS[acct]
        cid = get_contract(sym)

        # Manual flatten (close all) signal
        if sig == "FLAT":
            flatten_contract(acct_id, cid, timeout=10)
            logging.info(f"Manual flatten signal processed for {acct_id} {cid}")
            return

        now = datetime.now(CT)
        if in_get_flat(now):
            logging.info("In get-flat window, no trades processed")
            return

        # --- AI Overseer Routing ---
        if acct in AI_ENDPOINTS:
            positions = search_pos(acct_id)
            ai_url = AI_ENDPOINTS[acct]

            try:
                position_context = POSITION_MANAGER.get_position_context_for_ai(acct_id, cid)
            except Exception:
                position_context = None

            ai_decision = ai_trade_decision(
                acct,
                strat,
                sig,
                sym,
                size,
                alert,
                ai_url,
                positions=positions,
                position_context=position_context,
            )

            ai_signal = ai_decision.get("signal", "").upper()
            allowed_signals = {"BUY", "SELL", "HOLD", "FLAT"}

            if ai_signal not in allowed_signals:
                logging.info(f"AI blocked trade: {ai_decision.get('reason', 'No reason')}")
                return

            if ai_signal == "BUY":
                logging.info(f"AI signaled BUY: {ai_decision.get('reason', 'No reason')}")

            if ai_signal == "SELL":
                logging.info(f"AI signaled SELL: {ai_decision.get('reason', 'No reason')}")

            if ai_signal == "HOLD":
                logging.info(f"AI signaled HOLD: {ai_decision.get('reason', 'No reason')}")
                return

            if ai_signal == "FLAT":
                ai_sym = ai_decision.get("symbol", sym)
                ai_cid = get_contract(ai_sym)
                logging.info(f"AI signaled FLAT: {ai_decision.get('reason', 'No reason')}")
                logging.info(f"AI flatten signal processed for {acct_id} {ai_cid}")
                flatten_contract(acct_id, ai_cid, timeout=10)
                return

            # Overwrite user values with AI's preferred decision
            strat = ai_decision.get("strategy", strat)
            sig = ai_decision.get("signal", sig)
            sym = ai_decision.get("symbol", sym)
            try:
                size = int(ai_decision.get("size", size))
            except Exception:
                logging.warning("AI returned non-integer size=%r; keeping size=%s", ai_decision.get("size"), size)
            alert = ai_decision.get("alert", alert)
            ai_decision_id = ai_decision.get("ai_decision_id", ai_decision_id)
            cid = get_contract(sym)
            
        
        # --- Strategy Dispatch ---
        if strat != "simple":
            logging.error("Strategy '%s' is not implemented in this build (supported: simple)", strat)
            return

        run_simple(acct_id, sym, sig, size, alert, ai_decision_id)
    except Exception as e:
        import traceback
        logging.error(f"Exception in handle_webhook_logic: {e}\n{traceback.format_exc()}")

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
        logging.exception(f"Fatal error during startup: {e}")
