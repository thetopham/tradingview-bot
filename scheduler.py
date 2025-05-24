#scheduler.py

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging
import requests
from config import load_config
from api import get_market_conditions_summary

config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']
CT = pytz.timezone("America/Chicago")
TV_PORT = config['TV_PORT']

def start_scheduler(app):
    scheduler = BackgroundScheduler()
    
    # Original 5-minute cron job
    def cron_job():
        data = {
            "secret": WEBHOOK_SECRET,
            "strategy": "",
            "account": "beta",
            "signal": "",
            "symbol": "CON.F.US.MES.M25",
            "size": 3,
            "alert": f"APScheduler 5m"
        }
        try:
            response = requests.post(f'http://localhost:{TV_PORT}/webhook', json=data)
            logging.info(f"[APScheduler] HTTP POST call: {response.status_code} {response.text}")
        except Exception as e:
            logging.error(f"[APScheduler] HTTP POST failed: {e}")
    
    # Market analysis job - runs every 15 minutes
    def market_analysis_job():
        try:
            summary = get_market_conditions_summary()
            
            # Log warnings for dangerous market conditions
            if summary['regime'] == 'choppy' and summary['confidence'] > 80:
                logging.warning(f"⚠️ CHOPPY MARKET: Confidence {summary['confidence']}% - Trading not recommended")
            
            if summary['risk_level'] == 'high':
                logging.warning(f"⚠️ HIGH RISK CONDITIONS: {summary['key_factors']}")
                
            if summary['trend_alignment'] < 30:
                logging.warning(f"⚠️ POOR TREND ALIGNMENT: Only {summary['trend_alignment']}% aligned")
                
            # Log general market state
            logging.info(f"📊 Market Update: {summary['regime'].upper()} regime "
                        f"(conf: {summary['confidence']}%) | "
                        f"Risk: {summary['risk_level']} | "
                        f"Trade OK: {summary['trade_recommended']}")
                        
        except Exception as e:
            logging.error(f"[Market Analysis] Error: {e}")
    
    # Pre-session analysis jobs
    def pre_session_analysis(session_name):
        """Run analysis before each major session"""
        try:
            logging.info(f"🔔 Pre-{session_name} session analysis starting...")
            summary = get_market_conditions_summary()
            
            session_recommendations = {
                'LONDON': {
                    'good_regimes': ['trending_up', 'trending_down'],
                    'size_adjustment': 0
                },
                'NY_MORNING': {
                    'good_regimes': ['trending_up', 'trending_down', 'breakout'],
                    'size_adjustment': -1  # Reduce size due to volatility
                },
                'NY_AFTERNOON': {
                    'good_regimes': ['trending_up', 'trending_down', 'ranging'],
                    'size_adjustment': 0
                }
            }
            
            if session_name in session_recommendations:
                rec = session_recommendations[session_name]
                if summary['regime'] not in rec['good_regimes']:
                    logging.warning(f"⚠️ {session_name} session approaching but "
                                  f"{summary['regime']} regime may not be ideal")
                else:
                    logging.info(f"✅ {session_name} session conditions look favorable "
                               f"for {summary['regime']} regime")
                               
        except Exception as e:
            logging.error(f"[Pre-session Analysis] Error: {e}")
    
    # Schedule jobs
    scheduler.add_job(
        cron_job,
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=5, timezone=CT),
        id='5m_job',
        replace_existing=True
    )
    
    # Market analysis every 15 minutes
    scheduler.add_job(
        market_analysis_job,
        CronTrigger(minute='0,15,30,45', second=30, timezone=CT),
        id='market_analysis',
        replace_existing=True
    )
    
    # Pre-session analysis
    # London session prep (1:45 AM CT)
    scheduler.add_job(
        lambda: pre_session_analysis('LONDON'),
        CronTrigger(hour=1, minute=45, timezone=CT),
        id='pre_london',
        replace_existing=True
    )
    
    # NY Morning session prep (8:15 AM CT)
    scheduler.add_job(
        lambda: pre_session_analysis('NY_MORNING'),
        CronTrigger(hour=8, minute=15, timezone=CT),
        id='pre_ny_morning',
        replace_existing=True
    )
    
    # NY Afternoon session prep (12:45 PM CT)
    scheduler.add_job(
        lambda: pre_session_analysis('NY_AFTERNOON'),
        CronTrigger(hour=12, minute=45, timezone=CT),
        id='pre_ny_afternoon',
        replace_existing=True
    )
    
    scheduler.start()
    logging.info("[APScheduler] Scheduler started with market monitoring jobs")
    
    return scheduler
