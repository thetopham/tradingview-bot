# scheduler.py

import logging
from datetime import datetime
from typing import Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from auth import in_get_flat
from config import load_config
from market_state import build_market_state
from trigger_engine import decide
from position_manager import PositionManager
from api import get_supabase_client, get_contract

config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']
CT = pytz.timezone("America/Chicago")
TV_PORT = config['TV_PORT']
ACCOUNTS = config['ACCOUNTS']
DEFAULT_ACCOUNT = config['DEFAULT_ACCOUNT']
DEFAULT_SYMBOL = config['DEFAULT_SYMBOL']
DEFAULT_TRADE_SIZE = config['DEFAULT_TRADE_SIZE']
TRADING_ENABLED = config['TRADING_ENABLED']


def start_scheduler(app, position_manager: Optional[PositionManager] = None):
    scheduler = BackgroundScheduler()
    pm = position_manager or PositionManager(ACCOUNTS)

    def snapshot_job():
        now = datetime.now(CT)
        if in_get_flat(now):
            logging.info("[Scheduler] Skipping snapshot during get-flat window")
            return

        supabase = get_supabase_client()
        market_state = build_market_state(DEFAULT_SYMBOL, supabase_client=supabase)

        cid = get_contract(DEFAULT_SYMBOL)
        position_context = pm.get_position_context_for_ai(ACCOUNTS[DEFAULT_ACCOUNT], cid) if cid else None

        plan = decide(market_state, position_context, now=now)
        slope_summary = {
            tf: getattr(state, "normalized_slope", None)
            for tf, state in (market_state.timeframes.items() if market_state else [])
        }
        logging.info("[Scheduler] Market snapshot slopes: %s", slope_summary)
        logging.info("[Scheduler] Action plan: %s (%s)", plan.action, plan.reason_code)

    scheduler.add_job(snapshot_job, CronTrigger(minute='*/5', second=5))
    scheduler.start()
    logging.info("Scheduler started with 5-minute market snapshot job")
    return scheduler
