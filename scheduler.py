from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


#scheduler.py

def process_market_timeframe(timeframe):
    # Instead of requests.post(...)
    # Directly call the trading logic as a function
    from flask import Request
    data = {
        "secret": WEBHOOK_SECRET,
        "strategy": "brackmod",
        "account": "epsilon",
        "signal": "",
        "symbol": "CON.F.US.MES.M25",
        "size": 3,
        "alert": f"APScheduler {timeframe}"
    }
    # Call your webhook function directly, simulating a request
    with app.test_request_context('/webhook', json=data):
        response = tv_webhook()
        logging.info(f"[APScheduler] {timeframe} direct call: {response}")


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        process_market_timeframe, 
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=5, timezone=CT), 
        args=['5m'], 
        id='5m_job', 
        replace_existing=True
    )
    scheduler.start()
    logging.info("[APScheduler] Scheduler started with 5m job.")
    return scheduler
