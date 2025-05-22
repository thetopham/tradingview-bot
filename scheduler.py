#scheduler.py

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging
from config import load_config
import requests 

config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']

CT = pytz.timezone("America/Chicago")

def process_market_timeframe(app, data):
    with app.test_request_context('/webhook', json=data):
        response = app.view_functions['tv_webhook']()
        import logging
        logging.info(f"[APScheduler] direct call: {response}")

def start_scheduler(app):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz
    CT = pytz.timezone("America/Chicago")
    scheduler = BackgroundScheduler()
    def cron_job():
        data = {
            "secret": WEBHOOK_SECRET,
            "strategy": "brackmod",
            "account": "epsilon",
            "signal": "",
            "symbol": "CON.F.US.MES.M25",
            "size": 3,
            "alert": f"APScheduler 5m"
        }
        try:
            response = requests.post('http://localhost:5000/webhook', json=data)
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


