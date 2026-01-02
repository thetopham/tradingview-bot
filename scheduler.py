#scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging
import requests
from config import load_config

config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']
CT = pytz.timezone("America/Chicago")
TV_PORT = config['TV_PORT']

def start_scheduler(app, *, flatten_callback=None, flatten_timezone=None):
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

    if flatten_callback:
        scheduler.add_job(
            flatten_callback,
            CronTrigger(
                day_of_week='mon-fri',
                hour=14,
                minute=7,
                second=0,
                timezone=flatten_timezone or CT,
            ),
            id='daily_flatten',
            replace_existing=True
        )
    scheduler.start()
    logging.info(
        "[APScheduler] Scheduler started with 5m job%s.",
        " and daily flatten" if flatten_callback else ""
    )
    return scheduler

    return scheduler


