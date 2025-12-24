# scheduler.py

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
import pytz

from auth import in_get_flat
from config import load_config
from market_state import build_market_state
from position_manager import PositionManager
from trigger_engine import decide
from execution import send_entry, DEFAULT_TRADE_SIZE
from api import get_contract

config = load_config()
CT = pytz.timezone("America/Chicago")
ACCOUNTS = config['ACCOUNTS']
DEFAULT_ACCOUNT = config['DEFAULT_ACCOUNT']
DEFAULT_SYMBOL = config['DEFAULT_SYMBOL']

position_manager = None


def start_scheduler(app):
    global position_manager
    scheduler = BackgroundScheduler(timezone=CT)
    position_manager = PositionManager(ACCOUNTS)

    def evaluate_trigger():
        now = datetime.now(CT)
        if in_get_flat(now):
            logging.info("[Scheduler] Skipping evaluation during get-flat window")
            return

        acct_id = ACCOUNTS.get(DEFAULT_ACCOUNT)
        if not acct_id:
            logging.error("[Scheduler] Default account %s not found", DEFAULT_ACCOUNT)
            return

        try:
            contract_id = get_contract(DEFAULT_SYMBOL)
        except Exception:
            contract_id = None

        market_state = build_market_state(DEFAULT_SYMBOL)
        position_context = position_manager.get_position_context_for_ai(acct_id, contract_id)
        plan = decide(market_state, position_context, now=now)

        logging.info(
            "[Scheduler] Plan=%s reason=%s slopes=%s",
            plan.get("action"),
            plan.get("reason_code"),
            plan.get("details", {}).get("slopes"),
        )

        if plan.get("action") in {"BUY", "SELL"}:
            send_entry(plan["action"], acct_id, DEFAULT_SYMBOL, DEFAULT_TRADE_SIZE)
        else:
            logging.info("[Scheduler] HOLD - %s", plan.get("reason_code"))

    scheduler.add_job(
        evaluate_trigger,
        "interval",
        minutes=5,
        next_run_time=datetime.now(CT) + timedelta(seconds=10),
        id="market_trigger_job",
        coalesce=True,
        max_instances=1,
    )

    scheduler.start()
    logging.info("Scheduler started with 5-minute trigger evaluation")
    return scheduler
