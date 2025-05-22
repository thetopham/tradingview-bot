#scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging
import requests
from config import load_config
from signalr_listener import get_current_position

config = load_config()
acct = "epsilon"
acct_id = config['ACCOUNTS'][acct]
cid = config['OVERRIDE_CONTRACT_ID']

WEBHOOK_SECRET = config['WEBHOOK_SECRET']
CT = pytz.timezone("America/Chicago")
TV_PORT = config['TV_PORT']

def start_scheduler(app):
    scheduler = BackgroundScheduler()
    def cron_job():
        position = get_current_position(acct_id, cid)
        data = {
            "secret": WEBHOOK_SECRET,
            "strategy": "brackmod",
            "account": acct,
            "signal": "",  # Let AI choose
            "symbol": "CON.F.US.MES.M25",
            "size": 3,
            "alert": "APScheduler 5m",
            "position": position or {}
        }
        try:
            response = requests.post(f'http://localhost:{TV_PORT}/webhook', json=data)
            logging.info(f"[APScheduler] HTTP POST call: {response.status_code} {response.text}")
        except Exception as e:
            logging.error(f"[APScheduler] HTTP POST failed: {e}")
    scheduler.add_job(
        cron_job,
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=5, timezone=CT),
        id='5m_job',
        replace_existing=True
    )
    scheduler.start()
    logging.info("[APScheduler] Scheduler started with 5m job.")
    return scheduler

    return scheduler


