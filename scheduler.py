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
GET_FLAT_TZ = config['GET_FLAT_TZ']
ACCOUNTS = config['ACCOUNTS']


def flatten_open_positions():
    """Flatten any open positions across configured accounts."""

    for acct_name, acct_id in ACCOUNTS.items():
        try:
            positions = search_pos(acct_id)
        except Exception as exc:
            logging.error("[APScheduler] Unable to load positions for %s: %s", acct_name, exc)
            continue

        open_contract_ids = {pos.get("contractId") for pos in positions if pos.get("contractId")}
        if not open_contract_ids:
            logging.info("[APScheduler] No open positions to flatten for account %s", acct_name)
            continue

        for cid in open_contract_ids:
            try:
                flatten_contract(acct_id, cid, timeout=10)
                logging.info("[APScheduler] Flattened %s for account %s", cid, acct_name)
            except Exception as exc:
                logging.error("[APScheduler] Failed to flatten %s for account %s: %s", cid, acct_name, exc)


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
        flatten_open_positions,
        CronTrigger(day_of_week='mon-fri', hour=14, minute=7, timezone=GET_FLAT_TZ),
        id='daily_flatten_job',
        replace_existing=True
    )
    scheduler.start()
    logging.info("[APScheduler] Scheduler started with 5m job and daily flatten job.")
    return scheduler
