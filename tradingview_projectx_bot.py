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
    flatten_contract, get_contract, ai_trade_decision, search_pos, get_sim_adapter
    )
from position_manager import PositionManager
from auth import in_get_flat, authenticate, get_token, get_token_expiry, ensure_token, auth_lock
from dashboard import dashboard_bp
from threading import Thread
from datetime import datetime, timedelta, timezone
import logging
import os

# --- Logging/Config/Globals ---
setup_logging()
config = load_config()
BROKER_MODE = config['BROKER_MODE']
if BROKER_MODE != "sim":
    from strategies import run_simple
    from scheduler import start_scheduler
    from signalr_listener import launch_signalr_listener, annotate_trade_exit_intent
elif not config.get('WEBHOOK_SECRET'):
    raise RuntimeError("WEBHOOK_SECRET is required in sim mode")

TV_PORT         = config['TV_PORT']
WEBHOOK_SECRET  = config['WEBHOOK_SECRET']
ACCOUNTS        = config['ACCOUNTS']
DEFAULT_ACCOUNT = config['DEFAULT_ACCOUNT']
INVERTED_SIGNAL_ROUTING = config.get('INVERTED_SIGNAL_ROUTING', {})
LOCAL_TZ        = config['MT']
GET_FLAT_START  = config['GET_FLAT_START']
GET_FLAT_END    = config['GET_FLAT_END']

AI_TEST_ENDPOINTS = {
    "beta": config.get("N8N_OVERSEER_URL_TEST1"),
    "alpha": config.get("N8N_OVERSEER_URL_TEST2"),
    "gamma": config.get("N8N_OVERSEER_URL_TEST3"),
    "delta": config.get("N8N_OVERSEER_URL_TEST4"),
    "epsilon": config.get("N8N_OVERSEER_URL_TEST5"),
    "practice": config.get("N8N_OVERSEER_URL_TEST6"),
}
AI_TEST_ENDPOINTS.update({
    name: os.getenv(f"N8N_OVERSEER_URL_{name.upper()}") or AI_TEST_ENDPOINTS.get(name)
    for name in ACCOUNTS
})

POSITION_MANAGER = PositionManager(ACCOUNTS)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.register_blueprint(dashboard_bp)

# --- Health Check Route (optional, but recommended for uptime monitoring) ---
@app.route("/healthz")
def healthz():
    return jsonify(status="ok", broker_mode=BROKER_MODE, time=str(datetime.now(LOCAL_TZ)))

@app.route("/webhook", methods=["POST"])
def tv_webhook():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="unauthorized"), 403

    if BROKER_MODE == "sim":
        try:
            result = process_sim_webhook(data)
        except (ValueError, KeyError, TypeError) as exc:
            logging.warning("Simulated webhook rejected: %s", exc)
            return jsonify(error=str(exc)), 422
        return jsonify(status="simulated", account=result), 200

    # Respond immediately to TradingView/n8n
    Thread(target=handle_webhook_logic, args=(data,)).start()
    return jsonify(status="accepted", msg="Processing started"), 202


@app.route("/sim/events", methods=["GET"])
def sim_events():
    if BROKER_MODE != "sim":
        return jsonify(error="not found"), 404
    if request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return jsonify(error="unauthorized"), 403
    try:
        after_id = int(request.args.get("after_id", "0"))
        limit = int(request.args.get("limit", "100"))
        events = get_sim_adapter().events_after(after_id, limit)
    except ValueError as exc:
        return jsonify(error=str(exc)), 422
    return jsonify(events=events, next_cursor=events[-1]["id"] if events else after_id)


def process_sim_webhook(data):
    """Run the existing overseer against a closed bar, then advance v2 only."""
    account = (data.get("account") or DEFAULT_ACCOUNT).lower()
    if account not in ACCOUNTS:
        raise ValueError(f"unknown simulated account: {account}")
    bar = data.get("bar")
    if not isinstance(bar, dict) or not bar.get("timestamp"):
        raise ValueError("simulated webhook requires a closed bar with timestamp")
    adapter = get_sim_adapter()
    max_lag = config['SIM_MAX_BAR_LAG_SECONDS']
    if max_lag < 0:
        raise ValueError("SIM_MAX_BAR_LAG_SECONDS cannot be negative")
    if max_lag:
        from tvbot_v2.simulate.brokerless_executor import utc
        minutes = int(adapter.status(account)["timeframe"][:-1])
        bar_close = utc(str(bar["timestamp"])) + timedelta(minutes=minutes)
        if datetime.now(timezone.utc) - bar_close > timedelta(seconds=max_lag):
            raise ValueError("closed bar is too old for forward simulation")
    recorded = data.get("decision")
    if recorded is None:
        prior = adapter.processed_snapshot(account, str(bar["timestamp"]))
        if prior is not None:
            return prior
        ai_url = AI_TEST_ENDPOINTS.get(account)
        if ai_url:
            timeframe = adapter.status(account)["timeframe"]
            recorded = ai_trade_decision(
                account, data.get("strategy") or "simple", data.get("signal") or "HOLD",
                data.get("symbol") or "MES", data.get("size", 1),
                f"{timeframe} {data.get('alert') or ''}", ai_url,
                positions=adapter.positions(ACCOUNTS[account]),
                position_context=adapter.account_context(account),
            )
            if not isinstance(recorded, dict) or recorded.get("error"):
                recorded = {"signal": "HOLD", "size": 1,
                            "reason": "Overseer unavailable; holding", "source": "overseer_error"}
        else:
            recorded = {"signal": data.get("signal") or "HOLD", "size": data.get("size", 1),
                        "reason": data.get("reason", ""), "source": "webhook"}
    if not isinstance(recorded, dict):
        raise ValueError("decision must be an object")
    decision = dict(recorded)
    decision["size"] = int(decision.get("size", 1))
    decision.setdefault("source", "n8n_overseer" if AI_TEST_ENDPOINTS.get(account) else "webhook")
    return adapter.process_closed_bar(account, bar, decision)

def _invert_signal(signal: str) -> str:
    inverse_map = {"BUY": "SELL", "SELL": "BUY", "FLAT": "FLAT", "HOLD": "HOLD"}
    return inverse_map.get((signal or "").upper(), (signal or "").upper())


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
            mirror_account = INVERTED_SIGNAL_ROUTING.get(acct)
            mirror_acct_id = ACCOUNTS.get(mirror_account) if mirror_account else None
            if mirror_acct_id:
                annotate_trade_exit_intent(
                    mirror_acct_id,
                    cid,
                    exit_trigger="manual_flatten_webhook_inverted",
                    exit_reason=data.get("reason"),
                    exit_ai_decision_id=data.get("ai_decision_id"),
                )
                flatten_contract(mirror_acct_id, cid, timeout=10)
                logging.info("Manual flat mirrored to %s (%s)", mirror_account, mirror_acct_id)
            logging.info(f"Manual flatten signal processed for {acct_id} {cid}")
            return

        now = datetime.now(LOCAL_TZ)
        if in_get_flat(now):
            logging.info("In get-flat window, no trades processed")
            return

        # --- AI Overseer Routing ---
        ai_url = AI_TEST_ENDPOINTS.get(acct)
        if ai_url:
            positions = search_pos(acct_id)

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
                flatten_contract(acct_id, ai_cid, timeout=10)

                mirror_account = INVERTED_SIGNAL_ROUTING.get(acct)
                mirror_acct_id = ACCOUNTS.get(mirror_account) if mirror_account else None
                if mirror_acct_id:
                    annotate_trade_exit_intent(
                        mirror_acct_id,
                        ai_cid,
                        exit_trigger="ai_flatten_inverted",
                        exit_reason=ai_decision.get("reason"),
                        exit_ai_decision_id=ai_decision.get("ai_decision_id"),
                    )
                    flatten_contract(mirror_acct_id, ai_cid, timeout=10)
                    logging.info("AI flat mirrored to %s (%s)", mirror_account, mirror_acct_id)

                logging.info(f"AI flatten signal processed for {acct_id} {ai_cid}")
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

        mirror_account = INVERTED_SIGNAL_ROUTING.get(acct)
        mirror_acct_id = None
        mirror_sig = None
        if mirror_account:
            mirror_acct_id = ACCOUNTS.get(mirror_account)
            if mirror_acct_id is None:
                logging.error("Inverted routing misconfigured: source=%s target=%s (missing ACCOUNT_%s)", acct, mirror_account, mirror_account.upper())
            else:
                mirror_sig = _invert_signal(sig)
                logging.info(
                    "[INVERTED ROUTE] source_account=%s signal=%s -> target_account=%s inverted_signal=%s",
                    acct,
                    sig,
                    mirror_account,
                    mirror_sig,
                )

        # --- Strategy Dispatch ---
        if strat != "simple":
            logging.error("Strategy '%s' is not implemented in this build (supported: simple)", strat)
            return

        run_simple(acct_id, sym, sig, size, alert, ai_decision_id, prompt_version=prompt_version)

        if mirror_acct_id and mirror_sig in {"BUY", "SELL"}:
            run_simple(mirror_acct_id, sym, mirror_sig, size, alert, ai_decision_id, prompt_version=prompt_version)
    except Exception as e:
        import traceback
        logging.error(f"Exception in handle_webhook_logic: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    try:
        if BROKER_MODE == "sim":
            app.logger.info("Starting simulated broker webhook; ProjectX and SignalR disabled")
            app.run(host="127.0.0.1", port=TV_PORT, threaded=True)
        else:
            authenticate()
            signalr_listener = launch_signalr_listener(
                get_token=get_token,
                get_token_expiry=get_token_expiry,
                authenticate=authenticate,
                auth_lock=auth_lock
            )
            scheduler = start_scheduler(app)
            app.logger.info("Starting server.")
            app.run(host="0.0.0.0", port=TV_PORT, threaded=True)
    except Exception as e:
        logging.exception(f"Fatal error during startup: {e}")
