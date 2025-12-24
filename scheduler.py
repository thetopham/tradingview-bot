# scheduler.py

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
from datetime import datetime

import pytz

from config import load_config
from position_manager import PositionManager
from api import get_supabase_client, get_contract
from market_state import build_market_state
from trigger_engine import decide

config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']
CT = pytz.timezone("America/Chicago")
TV_PORT = config['TV_PORT']
ACCOUNTS = config['ACCOUNTS']
TRADING_ENABLED = config.get('TRADING_ENABLED', False)
DEFAULT_SYMBOL = config.get('MARKET_SYMBOL', 'MES')

position_manager = PositionManager(ACCOUNTS)


def start_scheduler(app):
    scheduler = BackgroundScheduler()
    supabase = get_supabase_client()

    def cron_job():
        from auth import in_get_flat

        now = datetime.now(CT)
        if in_get_flat(now):
            logging.info("[Scheduler] Skipping evaluation - in get-flat window")
            return

        try:
            market_state = build_market_state(supabase, DEFAULT_SYMBOL)
            logging.info(
                "[Scheduler] Market regime=%s slopes=%s price=%s", 
                market_state.get("regime"),
                market_state.get("slope"),
                market_state.get("price"),
            )

            for acct_name, acct_id in ACCOUNTS.items():
                try:
                    cid = get_contract(DEFAULT_SYMBOL)
                except Exception:
                    cid = None
                position_context = {}
                if cid:
                    position_context = position_manager.get_position_context_for_ai(acct_id, cid)
                plan = decide(market_state, position_context, in_get_flat(now), TRADING_ENABLED)
                logging.info(
                    "[Scheduler] account=%s action=%s reason=%s slopes=%s", 
                    acct_name,
                    plan.get("action"),
                    plan.get("reason_code"),
                    market_state.get("slope"),
                )
        except Exception as exc:
            logging.error(f"[Scheduler] Error during evaluation: {exc}", exc_info=True)

    scheduler.add_job(cron_job, CronTrigger(minute="*/5", timezone=CT))
    scheduler.start()
    return scheduler
