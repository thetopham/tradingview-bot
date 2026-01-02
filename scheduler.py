#scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging
import requests
from config import load_config
from api import flatten_contract, search_pos

config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']
CT = pytz.timezone("America/Chicago")
TV_PORT = config['TV_PORT']
ACCOUNTS = config['ACCOUNTS']


def _flatten_all_positions():
    """Flatten all open positions across accounts (used by daily cron)."""

    for acct_name, acct_id in ACCOUNTS.items():
        try:
            positions = search_pos(acct_id)
        except Exception as exc:
            logging.error("[APScheduler] Unable to fetch positions for %s: %s", acct_name, exc)
            continue

        if not positions:
            logging.info("[APScheduler] No open positions for %s", acct_name)
            continue

        seen_cids = set()
        for pos in positions:
            cid = pos.get("contractId") or pos.get("contractSymbol")
            if not cid or cid in seen_cids:
                continue

            seen_cids.add(cid)
            logging.info("[APScheduler] Auto-flattening %s for account %s", cid, acct_name)
            try:
                flatten_contract(acct_id, cid, timeout=15)
            except Exception as exc:
                logging.error("[APScheduler] Auto-flatten failed for %s (%s): %s", acct_name, cid, exc)

def start_scheduler(app):
    scheduler = BackgroundScheduler()
    def cron_job():
        data = {
            "secret": WEBHOOK_SECRET,
            "strategy": "",
            "account": "beta",
            "signal": "",
            "symbol": "CON.F.US.MES.H26",
            "size": 3,
            "alert": f"APScheduler 5m"
        }
        try:
            response = requests.post(f'http://localhost:{TV_PORT}/webhook', json=data)
            logging.info(f"[APScheduler] HTTP POST call: {response.status_code} {response.text}")
        except Exception as e:
            logging.error(f"[APScheduler] HTTP POST failed: {e}")
    scheduler.add_job(
        cron_job,
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=0, timezone=CT),
        id='5m_job',
        replace_existing=True
    )
    scheduler.add_job(
        _flatten_all_positions,
        CronTrigger(hour=15, minute=7, day_of_week='mon-fri', timezone=CT),
        id='daily_flatten_job',
        replace_existing=True,
    )
    scheduler.start()
    logging.info("[APScheduler] Scheduler started with 5m job and daily auto-flatten.")
    return scheduler
