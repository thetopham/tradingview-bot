#!/usr/bin/env python3
# tradingview_projectx_bot.py

"""
Main entry point for ProjectX Trading Bot.
Handles webhooks, AI decisions, trade execution, and scheduled processing.
"""

from datetime import time as dtime, timedelta
from api import ai_trade_decision_with_regime, get_market_conditions_summary
from market_regime import MarketRegime
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

MARKET_SESSIONS = {
    'ASIAN': {
        'start': dtime(19, 0),    # 7 PM CT
        'end': dtime(3, 0),       # 3 AM CT
        'characteristics': 'Lower volatility, trend continuation'
    },
    'LONDON': {
        'start': dtime(2, 0),     # 2 AM CT
        'end': dtime(11, 0),      # 11 AM CT
        'characteristics': 'High volatility, trend initiation'
    },
    'NY_MORNING': {
        'start': dtime(8, 30),    # 8:30 AM CT (Market open)
        'end': dtime(11, 0),      # 11 AM CT
        'characteristics': 'Highest volatility, breakouts'
    },
    'NY_LUNCH': {
        'start': dtime(11, 0),    # 11 AM CT
        'end': dtime(13, 0),      # 1 PM CT
        'characteristics': 'Low volatility, choppy'
    },
    'NY_AFTERNOON': {
        'start': dtime(13, 0),    # 1 PM CT
        'end': dtime(15, 0),      # 3 PM CT (Market close)
        'characteristics': 'Moderate volatility, trend resumption'
    },
    'NY_CLOSE': {
        'start': dtime(14, 30),   # 2:30 PM CT
        'end': dtime(15, 15),     # 3:15 PM CT
        'characteristics': 'Position squaring, volatility spike'
    }
}

def get_current_session(now=None):
    """Get current market session"""
    if now is None:
        now = datetime.now(CT)
    
    current_time = now.time()
    
    for session_name, session_info in MARKET_SESSIONS.items():
        start = session_info['start']
        end = session_info['end']
        
        # Handle sessions that cross midnight
        if start > end:
            if current_time >= start or current_time < end:
                return session_name, session_info
        else:
            if start <= current_time < end:
                return session_name, session_info
    
    return 'OFF_HOURS', {'characteristics': 'Market closed'}


AUTH_LOCK = threading.Lock()

app = Flask(__name__)

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
        strat = data.get("strategy", "bracket").lower()
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
        
        # Check if in get-flat window
        if in_get_flat(now):
            logging.info("In get-flat window, no trades processed")
            return
        
        # Get current market session
        session_name, session_info = get_current_session(now)
        logging.info(f"Current session: {session_name} - {session_info['characteristics']}")
        
        # Apply session-based rules
        if session_name == 'NY_LUNCH':
            logging.info("Skipping trade during NY lunch session (choppy market)")
            return
        
        if session_name == 'OFF_HOURS':
            logging.info("Market closed, skipping trade")
            return
        
        # Log market conditions periodically (every 5 minutes)
        if not hasattr(handle_webhook_logic, 'last_market_log'):
            handle_webhook_logic.last_market_log = now - timedelta(minutes=6)
        
        if now - handle_webhook_logic.last_market_log > timedelta(minutes=5):
            get_market_conditions_summary()
            handle_webhook_logic.last_market_log = now

        # --- Enhanced AI Overseer with Regime Detection ---
        if acct in AI_ENDPOINTS:
            ai_url = AI_ENDPOINTS[acct]
            
            # Use the new regime-aware AI decision function
            ai_decision = ai_trade_decision_with_regime(
                acct, strat, sig, sym, size, alert, ai_url
            )
            
            # Log regime information
            regime = ai_decision.get('regime', 'unknown')
            regime_confidence = ai_decision.get('regime_confidence', 0)
            logging.info(f"Market regime: {regime} (confidence: {regime_confidence}%)")
            
            if ai_decision.get("signal", "").upper() not in ("BUY", "SELL"):
                logging.info(f"AI blocked trade: {ai_decision.get('reason', 'No reason')}")
                return
                
            # Overwrite user values with AI's preferred decision
            strat = ai_decision.get("strategy", strat)
            sig = ai_decision.get("signal", sig)
            sym = ai_decision.get("symbol", sym)
            size = ai_decision.get("size", size)
            alert = ai_decision.get("alert", alert)
            ai_decision_id = ai_decision.get("ai_decision_id", ai_decision_id)
            
            # Apply session-based position sizing adjustments
            if session_name == 'NY_MORNING':
                # High volatility session - consider reducing size
                if regime in ['choppy', 'breakout']:
                    size = max(1, size - 1)
                    logging.info(f"Reduced position size to {size} for NY morning + {regime} regime")
            elif session_name == 'ASIAN':
                # Low volatility session - might increase size for trending markets
                if regime in ['trending_up', 'trending_down'] and regime_confidence > 80:
                    size = min(3, size + 1)
                    logging.info(f"Increased position size to {size} for Asian session trending market")

        # Check current positions before entry
        positions = search_pos(acct_id)
        open_pos = [p for p in positions if p["contractId"] == cid and p.get("size", 0) != 0]
        
        if not open_pos:
            cancel_all_stops(acct_id, cid)
        else:
            # Log current position for context
            total_size = sum(p.get("size", 0) for p in open_pos)
            logging.info(f"Current position size: {total_size}")

        # --- Strategy Dispatch ---
        logging.info(f"Executing {strat} strategy: {sig} {size} {sym} in {regime} regime")
        
        if strat == "bracket":
            run_bracket(acct_id, sym, sig, size, alert, ai_decision_id)
        elif strat == "brackmod":
            run_brackmod(acct_id, sym, sig, size, alert, ai_decision_id)
        elif strat == "pivot":
            run_pivot(acct_id, sym, sig, size, alert, ai_decision_id)
        else:
            logging.error(f"Unknown strategy '{strat}'")
            
    except Exception as e:
        import traceback
        logging.error(f"Exception in handle_webhook_logic: {e}\n{traceback.format_exc()}")

def scheduled_market_analysis():
    """Run market analysis every 15 minutes"""
    try:
        summary = get_market_conditions_summary()
        
        # You could also send this to Supabase or Discord
        if summary['regime'] == 'choppy' and summary['confidence'] > 80:
            logging.warning("High confidence choppy market detected - be cautious!")
            
    except Exception as e:
        logging.error(f"Error in scheduled market analysis: {e}")

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
