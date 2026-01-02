#scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
import requests
from config import load_config
from api import flatten_contract, search_pos

config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']
LOCAL_TZ = config['MT']
TV_PORT = config['TV_PORT']
ACCOUNTS = config['ACCOUNTS']

def start_scheduler(app):
    scheduler = BackgroundScheduler()

    def flatten_all_open_positions():
        for acct_name, acct_id in ACCOUNTS.items():
            positions = search_pos(acct_id)
            if not positions:
                logging.info("[APScheduler] %s: no open positions to flatten", acct_name)
                continue

            contract_ids = {
                pos.get("contractId") or pos.get("contractSymbol")
                for pos in positions
                if pos.get("contractId") or pos.get("contractSymbol")
            }

            if not contract_ids:
                logging.info("[APScheduler] %s: could not determine contract ids from positions", acct_name)
                continue

            for cid in contract_ids:
                logging.info("[APScheduler] Flattening %s for account %s", cid, acct_name)
                flatten_contract(acct_id, cid, timeout=10)

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
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=0, timezone=LOCAL_TZ),
        id='5m_job',
        replace_existing=True
    )

    scheduler.add_job(
        flatten_all_open_positions,
        CronTrigger(day_of_week='mon-fri', hour=14, minute=5, timezone=LOCAL_TZ),
        id='force_flat_job',
        replace_existing=True,
    )
    scheduler.start()
    logging.info("[APScheduler] Scheduler started with 5m job.")
    return scheduler

    return scheduler


