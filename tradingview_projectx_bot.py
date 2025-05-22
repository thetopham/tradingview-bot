#!/usr/bin/env python3
# tradingview_projectx_bot.py

import os
import time
import threading
import requests
from requests.adapters import HTTPAdapter
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timedelta, time as dtime
import pytz
import logging
import json
from logging.handlers import RotatingFileHandler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from signalr_listener import launch_signalr_listener, track_trade
from logging_config import setup_logging
from config import load_config
from api import post, place_market, place_limit, place_stop, search_open, cancel, search_pos, close_pos, search_trades, flatten_contract, cancel_all_stops, get_contract, ai_trade_decision, check_for_phantom_orders, log_trade_results_to_supabase
from strategies import run_bracket, run_brackmod, run_pivot
from scheduler import process_market_timeframe, start_scheduler
from auth import in_get_flat, authenticate, get_token, get_token_expiry, ensure_token

setup_logging()
config = load_config()

AI_ENDPOINTS = {
    "epsilon": N8N_AI_URL,
    "beta": N8N_AI_URL2,
    # add more as needed
}

STOP_LOSS_POINTS = 10.0
TP_POINTS        = [2.5, 5.0, 10.0]
OVERRIDE_CONTRACT_ID = "CON.F.US.MES.M25"

CT = pytz.timezone("America/Chicago")
GET_FLAT_START = dtime(15, 7)
GET_FLAT_END   = dtime(17, 0)

session = requests.Session()
adapter = HTTPAdapter(pool_maxsize=10, max_retries=3)
session.mount("https://", adapter)

# ─── Flask App Setup ──────────────────────────────────
app = Flask(__name__)



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

    if sig == "FLAT":
        ok = flatten_contract(acct_id, cid, timeout=10)
        status = "ok" if ok else "error"
        code = 200 if ok else 500
        return jsonify(status=status, strategy=strat, message="flattened"), code

    now = datetime.now(CT)
    if in_get_flat(now):
        return jsonify(status="ok", strategy=strat, message="in get-flat window, no trades"), 200

    # ----- Multi AI overseer logic -----
    if acct in AI_ENDPOINTS:
        ai_url = AI_ENDPOINTS[acct]
        ai_decision = ai_trade_decision(acct, strat, sig, sym, size, alert, ai_url)
        # If AI says HOLD or error, block trade
        if ai_decision.get("signal", "").upper() not in ("BUY", "SELL"):
            return jsonify(status="blocked", reason=ai_decision.get("reason", "No reason"), ai_decision=ai_decision), 200
        # Overwrite with AI's preferred strategy, symbol, etc.
        strat = ai_decision.get("strategy", strat)
        sig = ai_decision.get("signal", sig)
        sym = ai_decision.get("symbol", sym)
        size = ai_decision.get("size", size)
        alert = ai_decision.get("alert", alert)
        ai_decision_id = ai_decision.get("ai_decision_id", ai_decision_id)

    # ----- Continue for all accounts -----
    if strat == "bracket":
        return run_bracket(acct_id, sym, sig, size, alert, ai_decision_id)
    elif strat == "brackmod":
        return run_brackmod(acct_id, sym, sig, size, alert, ai_decision_id)
    elif strat == "pivot":
        return run_pivot(acct_id, sym, sig, size, alert, ai_decision_id)
    else:
        return jsonify(error=f"Unknown strategy '{strat}'"), 400

if __name__ == "__main__":
    authenticate()
    print(f"DEBUG: Got token: {_token[:12]}..., expiry: {_token_expiry}, now: {time.time()}")
    if not _token:
        raise RuntimeError("Token is None after authentication!")

    signalr_listener = launch_signalr_listener(
        get_token=get_token, 
        get_token_expiry=get_token_expiry,
        authenticate=authenticate,         # <-- ADD THIS
        auth_lock=auth_lock                # <-- AND THIS
    )
    scheduler = start_scheduler()
    app.logger.info("Starting server.")
    app.run(host="0.0.0.0", port=TV_PORT, threaded=True)





