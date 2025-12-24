import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from auth import in_get_flat
from config import load_config
from market_state import build_market_state
from position_manager import PositionManager

config = load_config()
CT = config['CT']
ACCOUNTS = config['ACCOUNTS']
DEFAULT_SYMBOL = config.get('DEFAULT_SYMBOL', 'MES')


def start_scheduler(app):
    scheduler = BackgroundScheduler()
    position_manager = PositionManager(ACCOUNTS)

    def market_state_snapshot():
        now = datetime.now(CT)
        if in_get_flat(now):
            logging.info("[Scheduler] Skipping snapshot - in get-flat window")
            return
        try:
            state = build_market_state(DEFAULT_SYMBOL)
            tf_slopes = {
                tf: tf_state.get('normalized_slope')
                for tf, tf_state in state.get('timeframes', {}).items()
            }
            logging.info("[Scheduler] Market snapshot for %s -> %s", DEFAULT_SYMBOL, tf_slopes)
        except Exception as exc:
            logging.error(f"[Scheduler] Snapshot failed: {exc}")

    def account_health_check():
        try:
            for account_name, acct_id in ACCOUNTS.items():
                metrics = position_manager.get_account_state(acct_id)
                logging.info(
                    "[Scheduler] Account %s | can_trade=%s daily_pnl=%.2f risk=%s",
                    account_name,
                    metrics.get('can_trade'),
                    metrics.get('daily_pnl'),
                    metrics.get('risk_level'),
                )
        except Exception as exc:
            logging.error(f"[Scheduler] Account health error: {exc}")

    def cleanup_metadata_wrapper():
        try:
            from signalr_listener import cleanup_stale_metadata

            removed = cleanup_stale_metadata(max_age_hours=12)
            if removed:
                logging.info(f"[Scheduler] Removed {removed} stale trade metadata entries")
        except Exception as exc:
            logging.error(f"[Scheduler] Metadata cleanup failed: {exc}")

    scheduler.add_job(market_state_snapshot, 'interval', minutes=5, id='market_state_snapshot')
    scheduler.add_job(account_health_check, 'cron', minute='*/30', id='account_health_check')
    scheduler.add_job(cleanup_metadata_wrapper, 'cron', minute=5, id='metadata_cleanup')

    scheduler.start()
    logging.info("Scheduler started with market-state and health checks")
    return scheduler

