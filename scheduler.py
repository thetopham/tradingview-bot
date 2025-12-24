from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
import pytz
from datetime import datetime

from auth import in_get_flat
from config import load_config
from api import get_supabase_client, get_contract
from market_state import build_market_state
from position_manager import PositionManager
from trigger_engine import decide

config = load_config()
CT = pytz.timezone("America/Chicago")
ACCOUNTS = config['ACCOUNTS']
TRADING_ENABLED = config.get('TRADING_ENABLED', False)
MARKET_SYMBOL = config.get('MARKET_SYMBOL', 'MES')

position_manager = None


def start_scheduler(app):
    global position_manager
    scheduler = BackgroundScheduler()
    position_manager = PositionManager(ACCOUNTS)
    supabase = get_supabase_client()

    def market_snapshot_job():
        if in_get_flat(datetime.now(CT)):
            logging.info("[Scheduler] Skipping snapshot - in get-flat window")
            return
        try:
            market_state = build_market_state(supabase, symbol=MARKET_SYMBOL)
            logging.info(
                "[Scheduler] Market snapshot regime=%s slopes=%s price=%s",
                market_state.get("regime"),
                market_state.get("slope"),
                market_state.get("price"),
            )
            contract_id = get_contract(MARKET_SYMBOL)
            if not contract_id:
                logging.error("[Scheduler] Could not resolve contract for %s", MARKET_SYMBOL)
                return
            for account_name, acct_id in ACCOUNTS.items():
                position_context = position_manager.get_position_context_for_ai(acct_id, contract_id)
                plan = decide(market_state, position_context, False, TRADING_ENABLED)
                logging.info(
                    "[Scheduler] account=%s action=%s reason=%s trading_enabled=%s",
                    account_name,
                    plan.get("action"),
                    plan.get("reason_code"),
                    TRADING_ENABLED,
                )
        except Exception as exc:
            logging.error("[Scheduler] market snapshot failed: %s", exc, exc_info=True)

    scheduler.add_job(
        market_snapshot_job,
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=5, timezone=CT),
        id='market_snapshot',
        replace_existing=True,
    )

    scheduler.start()
    return scheduler
