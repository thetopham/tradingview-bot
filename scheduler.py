from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
from datetime import datetime
import pytz

from config import load_config
from position_manager import PositionManager
from auth import in_get_flat
from market_state import build_market_state
from trigger_engine import decide
from execution import send_entry
from api import get_contract

config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']
CT = pytz.timezone("America/Chicago")
TV_PORT = config['TV_PORT']
ACCOUNTS = config['ACCOUNTS']
TRADING_ENABLED = config['TRADING_ENABLED']
DEFAULT_SIZE = config['DEFAULT_SIZE']
MARKET_SYMBOL = config['MARKET_SYMBOL']

position_manager = None


def start_scheduler(app):
    global position_manager
    scheduler = BackgroundScheduler(timezone=CT)
    position_manager = PositionManager(ACCOUNTS)

    def reduction_job():
        now = datetime.now(CT)
        in_flat = in_get_flat(now)
        try:
            market_state = build_market_state()
            logging.info(
                "[Scheduler] MarketState regime=%s slopes=%s price=%s",
                market_state.get('regime'),
                market_state.get('slope'),
                market_state.get('price'),
            )
        except Exception as exc:
            logging.error(f"[Scheduler] Failed to build market state: {exc}")
            return

        try:
            cid = get_contract(MARKET_SYMBOL)
        except Exception as exc:
            logging.error(f"[Scheduler] Failed to resolve contract for {MARKET_SYMBOL}: {exc}")
            return

        for account_name, acct_id in ACCOUNTS.items():
            try:
                position_context = position_manager.get_position_context_for_ai(acct_id, cid)
                action_plan = decide(market_state, position_context, in_flat, TRADING_ENABLED)
                logging.info(
                    "[Scheduler] account=%s regime=%s slopes=%s action=%s reason=%s trading_enabled=%s",
                    account_name,
                    market_state.get('regime'),
                    market_state.get('slope'),
                    action_plan.get('action'),
                    action_plan.get('reason_code'),
                    TRADING_ENABLED,
                )
                if action_plan.get('action') in ("BUY", "SELL"):
                    send_entry(action_plan['action'], acct_id, MARKET_SYMBOL, DEFAULT_SIZE, TRADING_ENABLED)
            except Exception as exc:
                logging.error(f"[Scheduler] Error evaluating account {account_name}: {exc}")

    scheduler.add_job(
        reduction_job,
        CronTrigger(minute='*/5', second=5, timezone=CT),
        name="reduction-eval",
        replace_existing=True,
    )

    scheduler.start()
    logging.info("Background scheduler started with reduction job only")
    return scheduler
