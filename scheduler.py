import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
import pytz

from config import load_config
from position_manager import PositionManager
from api import get_contract
from market_state import build_market_state
from trigger_engine import decide

config = load_config()
CT = pytz.timezone("America/Chicago")
ACCOUNTS = config['ACCOUNTS']
DEFAULT_SYMBOL = config.get('DEFAULT_SYMBOL', 'MES')
TRADING_ENABLED = config.get('TRADING_ENABLED', False)


def start_scheduler(app):
    scheduler = BackgroundScheduler()
    position_manager = PositionManager(ACCOUNTS)

    def market_snapshot_job():
        try:
            account_name = config['DEFAULT_ACCOUNT']
            acct_id = ACCOUNTS[account_name]
            contract_id = get_contract(DEFAULT_SYMBOL)
            context = position_manager.get_position_context_for_ai(acct_id, contract_id)
            market_state = build_market_state(DEFAULT_SYMBOL)
            plan = decide(market_state, context)
            logging.info(
                "[Scheduler] Snapshot %s plan=%s reason=%s trading_enabled=%s",
                DEFAULT_SYMBOL,
                plan.action,
                plan.reason_code,
                TRADING_ENABLED,
            )
        except Exception as exc:
            logging.error("[Scheduler] snapshot failed: %s", exc, exc_info=True)

    scheduler.add_job(market_snapshot_job, 'interval', minutes=5, next_run_time=datetime.now(CT))
    scheduler.start()
    logging.info("Scheduler started with market snapshot job")
    return scheduler
