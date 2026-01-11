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
from auth import in_get_flat, authenticate, get_token, get_token_expiry, ensure_token, auth_lock
from signalr_listener import launch_signalr_listener, annotate_trade_exit_intent
from dashboard import dashboard_bp
from regime_classifier import RegimeClassifier
from threading import Thread
from datetime import datetime
import logging

# --- Logging/Config/Globals ---
setup_logging()
config = load_config()

TV_PORT         = config['TV_PORT']
WEBHOOK_SECRET  = config['WEBHOOK_SECRET']
ACCOUNTS        = config['ACCOUNTS']
DEFAULT_ACCOUNT = config['DEFAULT_ACCOUNT']
LOCAL_TZ        = config['MT']
GET_FLAT_START  = config['GET_FLAT_START']
GET_FLAT_END    = config['GET_FLAT_END']
BROKER_MODE     = config.get('BROKER_MODE', 'live').lower()

AI_TEST_ENDPOINTS = {
    "beta": config.get("N8N_OVERSEER_URL_TEST1"),
    "alpha": config.get("N8N_OVERSEER_URL_TEST2"),
    "gamma": config.get("N8N_OVERSEER_URL_TEST3"),
    "delta": config.get("N8N_OVERSEER_URL_TEST4"),
    "epsilon": config.get("N8N_OVERSEER_URL_TEST5"),
    "practice": config.get("N8N_OVERSEER_URL_TEST6"),
}

POSITION_MANAGER = PositionManager(ACCOUNTS)

# --- Regime Classifier ---
REGIME_CLASSIFIER = RegimeClassifier(
    supabase_url=config.get("SUPABASE_URL"),
    supabase_key=config.get("SUPABASE_KEY"),
    table_5m=config.get("REGIME_TABLE_5M", "tv_datafeed_5m"),
    table_htf=config.get("REGIME_TABLE_HTF", "tv_datafeed_30m"),
    table_htf_fallback=config.get("REGIME_TABLE_HTF_FALLBACK", "tv_datafeed_15m"),
    lookback_bars=config.get("REGIME_LOOKBACK_BARS", 140),
    er_lookback=config.get("REGIME_ER_LOOKBACK", 12),
    atr_pctl_lookback=config.get("REGIME_ATR_PCTL_LOOKBACK", 50),
    er_min=config.get("REGIME_ER_MIN", 0.35),
    atr_pctl_max=config.get("REGIME_ATR_PCTL_MAX", 0.25),
    ema_spread_atr_min=config.get("REGIME_EMA_SPREAD_ATR_MIN", 0.30),
    slope_atr_min=config.get("REGIME_SLOPE_ATR_MIN", 0.50),
    atr_min_points=config.get("REGIME_ATR_MIN_POINTS"),
    atr_max_points=config.get("REGIME_ATR_MAX_POINTS"),
    require_htf_align=config.get("REGIME_REQUIRE_HTF_ALIGN", True),
    fail_closed=config.get("REGIME_FAIL_CLOSED", True),
    cache_ttl_s=10,
)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.register_blueprint(dashboard_bp)

# --- Health Check Route (optional, but recommended for uptime monitoring) ---
@app.route("/healthz")
def healthz():
    return jsonify(status="ok", time=str(datetime.now(LOCAL_TZ)))

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
        prompt_version = None

        if acct not in ACCOUNTS:
            logging.error(f"Unknown account '{acct}'")
            return

        acct_id = ACCOUNTS[acct]
        cid = get_contract(sym)

        # Manual flatten (close all) signal
        if sig == "FLAT":
            annotate_trade_exit_intent(
                acct_id,
                cid,
                exit_trigger="manual_flatten_webhook",
                exit_reason=data.get("reason"),
                exit_ai_decision_id=data.get("ai_decision_id"),
            )
            flatten_contract(acct_id, cid, timeout=10)
            logging.info(f"Manual flatten signal processed for {acct_id} {cid}")
            return

        now = datetime.now(LOCAL_TZ)
        if in_get_flat(now):
            logging.info("In get-flat window, no trades processed")
            return

        # --- Regime prefilter (prevents AI calls in chop/range) ---
        pre_rr = None
        regime_label = None
        regime_blocked = False

        if config.get("REGIME_FILTER_ENABLED", True):
            desired = sig if sig in {"BUY", "SELL"} else None
            pre_rr = REGIME_CLASSIFIER.classify(sym, desired_signal=desired)
            regime_label = pre_rr.regime
            if not pre_rr.ok:
                regime_blocked = True
                logging.info(
                    "[REGIME PREFILTER BLOCK] %s %s | %s | metrics=%s",
                    desired or "ANY",
                    sym,
                    pre_rr.reason,
                    {
                        k: pre_rr.metrics.get(k)
                        for k in (
                            "direction",
                            "er",
                            "atr",
                            "atr_pctl",
                            "ema_spread_atr",
                            "slope_norm",
                            "macd_ok",
                            "bb_width",
                            "htf_ok",
                            "htf_table",
                            "ts",
                        )
                    },
                )
                # If the webhook explicitly requests BUY/SELL, block immediately.
                if desired is not None:
                    return


        # --- AI Overseer Routing ---
        ai_url = AI_TEST_ENDPOINTS.get(acct)
        if ai_url:
            positions = search_pos(acct_id)
            has_position = any(
                (p.get("contractId") == cid) and (p.get("size") or 0) > 0
                for p in (positions or [])
            )

            # If regime is blocked and we do NOT have a position to manage,
            # skip the AI call entirely (saves tokens and prevents chop overtrading).
            if regime_blocked and not has_position:
                logging.info(
                    "[REGIME PREFILTER] blocked -> skipping AI call (no open position) | %s | %s",
                    sym,
                    pre_rr.reason if pre_rr else "unknown",
                )
                return

            try:
                position_context = POSITION_MANAGER.get_position_context_for_ai(acct_id, cid)
            except Exception:
                position_context = None

            route_label = "default"
            if acct == "beta":
                route_label = "TEST1"
            elif acct == "alpha":
                route_label = "TEST2"
            elif acct == "gamma":
                route_label = "TEST3"
            elif acct == "delta":
                route_label = "TEST4"
            elif acct == "epsilon":
                route_label = "TEST5"
            elif acct == "practice":
                route_label = "TEST6"

            safe_url = ai_url.split("?")[0] if ai_url else "unset"
            logging.info("[AI ROUTE] account=%s -> %s url=%s", acct, route_label, safe_url)

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

            # If regime is blocked but we have an open position, allow only HOLD/FLAT.
            if regime_blocked and ai_signal in {"BUY", "SELL"}:
                logging.info(
                    "[REGIME BLOCK] In blocked regime; ignoring AI entry signal %s and forcing HOLD (position_management_only)",
                    ai_signal,
                )
                return

            # If regime is OK, ensure AI entry aligns with detected direction.
            if (not regime_blocked) and pre_rr is not None and ai_signal in {"BUY", "SELL"}:
                if ai_signal == "BUY" and pre_rr.direction != "UP":
                    logging.info("[REGIME BLOCK] AI BUY conflicts with regime direction=%s; blocking", pre_rr.direction)
                    return
                if ai_signal == "SELL" and pre_rr.direction != "DOWN":
                    logging.info("[REGIME BLOCK] AI SELL conflicts with regime direction=%s; blocking", pre_rr.direction)
                    return

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
                annotate_trade_exit_intent(
                    acct_id,
                    ai_cid,
                    exit_trigger="ai_flatten",
                    exit_reason=ai_decision.get("reason"),
                    exit_ai_decision_id=ai_decision.get("ai_decision_id"),
                )
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
            prompt_version = ai_decision.get("prompt_version")
            cid = get_contract(sym)

        # --- Regime enforcement (final signal) ---
        if config.get("REGIME_FILTER_ENABLED", True) and sig in {"BUY", "SELL"}:
            rr = pre_rr
            if rr is None:
                rr = REGIME_CLASSIFIER.classify(sym, desired_signal=sig)
                regime_label = rr.regime

            if not rr.ok:
                logging.info(
                    "[REGIME BLOCK] %s %s | %s | metrics=%s",
                    sig,
                    sym,
                    rr.reason,
                    {
                        k: rr.metrics.get(k)
                        for k in (
                            "direction",
                            "er",
                            "atr",
                            "atr_pctl",
                            "ema_spread_atr",
                            "slope_norm",
                            "macd_ok",
                            "bb_width",
                            "htf_ok",
                            "htf_table",
                            "ts",
                        )
                    },
                )
                return

            # Extra safety: ensure final signal matches detected direction.
            if sig == "BUY" and rr.direction != "UP":
                logging.info("[REGIME BLOCK] BUY %s | direction=%s mismatch", sym, rr.direction)
                return
            if sig == "SELL" and rr.direction != "DOWN":
                logging.info("[REGIME BLOCK] SELL %s | direction=%s mismatch", sym, rr.direction)
                return

            logging.info(
                "[REGIME OK] %s %s | regime=%s | metrics=%s",
                sig,
                sym,
                rr.regime,
                {
                    k: rr.metrics.get(k)
                    for k in (
                        "direction",
                        "er",
                        "atr",
                        "atr_pctl",
                        "ema_spread_atr",
                        "slope_norm",
                        "macd_ok",
                        "bb_width",
                        "htf_ok",
                        "htf_table",
                        "ts",
                    )
                },
            )

        # --- Strategy Dispatch ---
        if strat != "simple":
            logging.error("Strategy '%s' is not implemented in this build (supported: simple)", strat)
            return

        run_simple(acct_id, sym, sig, size, alert, ai_decision_id, prompt_version=prompt_version, regime=regime_label)
    except Exception as e:
        import traceback
        logging.error(f"Exception in handle_webhook_logic: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    try:
        if BROKER_MODE != "sim":
            authenticate()
            signalr_listener = launch_signalr_listener(
                get_token=get_token,
                get_token_expiry=get_token_expiry,
                authenticate=authenticate,
                auth_lock=auth_lock
            )
        else:
            signalr_listener = None
        scheduler = start_scheduler(app)
        app.logger.info("Starting server.")
        app.run(host="0.0.0.0", port=TV_PORT, threaded=True)
    except Exception as e:
        logging.exception(f"Fatal error during startup: {e}")
