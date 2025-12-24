from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
from datetime import datetime

from config import load_config
from position_manager import PositionManager
from auth import in_get_flat
from api import get_supabase_client, get_contract
from market_state import build_market_state
from trigger_engine import decide

config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']
CT = config['CT']
TV_PORT = config['TV_PORT']
ACCOUNTS = config['ACCOUNTS']
TRADING_ENABLED = config.get('TRADING_ENABLED', False)
MARKET_SYMBOL = config.get('MARKET_SYMBOL', config.get('DEFAULT_SYMBOL', 'MES'))
DEFAULT_SIZE = config.get('DEFAULT_SIZE', 1)

position_manager = None


def start_scheduler(app):
    global position_manager
    scheduler = BackgroundScheduler()
    position_manager = PositionManager(ACCOUNTS)

    def cron_job():
        now = datetime.now(CT)
        if in_get_flat(now):
            logging.info("[Scheduler] Skipping evaluation - in get-flat window")
            return

        try:
            supabase = get_supabase_client()
            market_state = build_market_state(supabase, symbol=MARKET_SYMBOL)
        except Exception as exc:
            logging.error("[Scheduler] Failed to build market state: %s", exc)
            return

        logging.info(
            "[Scheduler] Market regime=%s slopes=%s trading_enabled=%s",
            market_state.get("regime", "unknown") if market_state else "unknown",
            market_state.get("slope") if market_state else {},
            TRADING_ENABLED,
        )

        try:
            cid = get_contract(MARKET_SYMBOL)
        except Exception:
            cid = None

        for account_name, acct_id in ACCOUNTS.items():
            position_context = position_manager.get_position_context_for_ai(acct_id, cid) if cid else {}
            plan = decide(market_state, position_context, in_get_flat(now), TRADING_ENABLED)
            logging.info(
                "[Scheduler] Account=%s action=%s reason=%s slopes=%s",
                account_name,
                plan.get("action"),
                plan.get("reason_code"),
                market_state.get("slope") if market_state else {},
            )

    scheduler.add_job(cron_job, CronTrigger(minute='*/5', timezone=CT))
    scheduler.start()
    logging.info("Scheduler started with 5-minute market evaluation job")
    return scheduler

