#scheduler.py

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging
import requests
from config import load_config
from api import get_market_conditions_summary
from position_manager import PositionManager
from strategies import run_bracket
from api import get_supabase_client, fetch_multi_timeframe_analysis
from datetime import datetime, timedelta

config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']
CT = pytz.timezone("America/Chicago")
TV_PORT = config['TV_PORT']
ACCOUNTS = config['ACCOUNTS']

# Global position manager instance
position_manager = None

def start_scheduler(app):
    global position_manager
    scheduler = BackgroundScheduler()
    
    # Initialize position manager
    position_manager = PositionManager(ACCOUNTS)
    
    # Original 5-minute cron job
    def cron_job():
        """Runs 5 seconds after each 5-min candle close to fetch fresh charts AND update regime"""
        logging.info("[APScheduler] Fetching fresh charts and updating regime after candle close")
    
        try:
            # Clear any existing cache first
            supabase = get_supabase_client()
            cutoff = (datetime.utcnow() - timedelta(seconds=30)).isoformat()
            supabase.table('market_regime_cache').delete().lt('timestamp', cutoff).execute()
        
            # Get n8n base URL
            n8n_base_url = config.get('N8N_AI_URL', '').split('/webhook/')[0]
        
            # Force fetch fresh charts AND regime analysis
            market_analysis = fetch_multi_timeframe_analysis(
                n8n_base_url,
                timeframes=['5m', '15m', '1h'], 
                cache_minutes=0,  # Don't use cache
                force_refresh=True  # Force fresh fetch
            )
        
            # Log regime update
            regime = market_analysis.get('regime_analysis', {})
            logging.info(f"[APScheduler] Regime updated: {regime.get('primary_regime', 'unknown')} "
                        f"(confidence: {regime.get('confidence', 0)}%)")
        
            # Check for regime changes and alerts
            if regime.get('primary_regime') == 'choppy' and regime.get('confidence', 0) > 80:
                logging.warning(f"⚠️ CHOPPY MARKET ALERT: High confidence choppy conditions detected!")
        
        except Exception as e:
            logging.error(f"[APScheduler] Chart/regime fetch failed: {e}")
    
        # Then run the normal webhook
        data = {
            "secret": WEBHOOK_SECRET,
            "strategy": "",
            "account": "beta",
            "signal": "",
            "symbol": "CON.F.US.MES.M25",
            "size": 3,
            "alert": f"APScheduler 5m - candle close"
        }
        try:
            response = requests.post(f'http://localhost:{TV_PORT}/webhook', json=data)
            logging.info(f"[APScheduler] Webhook call: {response.status_code}")
        except Exception as e:
            logging.error(f"[APScheduler] Webhook failed: {e}")
    
    # Market analysis job - runs every 15 minutes
    # Market analysis job - runs every 15 minutes
    def market_analysis_job():
        try:
            from api import get_market_conditions_summary
            summary = get_market_conditions_summary(force_refresh=False)  # Use cache, don't force refresh
        
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
    
    # Position management job - runs every 2 minutes
    def position_management_job():
        """Manage existing positions and look for opportunities"""
        try:
            logging.info("🔄 Running position management check...")
            
            from api import get_contract
            cid = get_contract("CON.F.US.MES.M25")
            
            for account_name, acct_id in ACCOUNTS.items():
                try:
                    # Get position state
                    position_state = position_manager.get_position_state(acct_id, cid)
                    
                    # If we have a position, manage it
                    if position_state['has_position']:
                        logging.info(f"Managing position for {account_name}: "
                                   f"{position_state['size']} contracts, "
                                   f"P&L: ${position_state['current_pnl']:.2f}")
                        
                        result = position_manager.manage_position(acct_id, cid, position_state)
                        if result['action'] != 'none':
                            logging.info(f"Position action taken: {result}")
                    
                    # If no position, check for opportunities (only during market hours)
                    else:
                        from auth import in_get_flat
                        from datetime import datetime
                        
                        now = datetime.now(CT)
                        if not in_get_flat(now):
                            opportunity = position_manager.scan_for_opportunities(acct_id, account_name)
                            if opportunity:
                                logging.info(f"🎯 Autonomous trade opportunity detected for {account_name}")
                                # Execute the trade
                                from threading import Thread
                                Thread(target=execute_autonomous_trade, args=(opportunity,)).start()
                                
                except Exception as e:
                    logging.error(f"[Position Management] Error for {account_name}: {e}")
                    
        except Exception as e:
            logging.error(f"[Position Management] General error: {e}")
    
    # Account health check - runs every 30 minutes
    def account_health_check():
        """Monitor account health and risk metrics"""
        try:
            logging.info("🏥 Running account health check...")
            
            for account_name, acct_id in ACCOUNTS.items():
                try:
                    account_state = position_manager.get_account_state(acct_id)
                    
                    # Log account metrics
                    logging.info(f"Account {account_name}: "
                               f"Daily P&L: ${account_state['daily_pnl']:.2f} | "
                               f"Win Rate: {account_state['win_rate']:.1%} | "
                               f"Risk: {account_state['risk_level']} | "
                               f"Can Trade: {account_state['can_trade']}")
                    
                    # Warnings
                    if not account_state['can_trade']:
                        logging.warning(f"⛔ Account {account_name} CANNOT TRADE - Risk limits hit")
                    
                    if account_state['risk_level'] == 'high':
                        logging.warning(f"⚠️ Account {account_name} at HIGH RISK")
                        
                except Exception as e:
                    logging.error(f"[Account Health] Error for {account_name}: {e}")
                    
        except Exception as e:
            logging.error(f"[Account Health] General error: {e}")
    
    # Pre-session analysis jobs
    def pre_session_analysis(session_name):
        """Run analysis before each major session"""
        try:
            logging.info(f"🔔 Pre-{session_name} session analysis starting...")
            summary = get_market_conditions_summary(force_refresh=True)
            
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
                    
                # Check account readiness
                for account_name, acct_id in ACCOUNTS.items():
                    account_state = position_manager.get_account_state(acct_id)
                    if not account_state['can_trade']:
                        logging.warning(f"Account {account_name} not ready for {session_name} session")
                               
        except Exception as e:
            logging.error(f"[Pre-session Analysis] Error: {e}")

    def monitor_data_feed():
        """Monitor data feed health and log price updates"""
        try:
            from api import get_current_market_price, get_spread_and_mid_price
        
            # Get current price
            price, source = get_current_market_price(max_age_seconds=60)
        
            if price:
                # Get additional price info
                price_info = get_spread_and_mid_price()
            
                # Only log if there's an open position
                has_positions = False
                for account_name, acct_id in ACCOUNTS.items():
                    positions = position_manager.get_position_state(acct_id, get_contract("CON.F.US.MES.M25"))
                    if positions['has_position']:
                        has_positions = True
                        break
            
                if has_positions:
                    logging.info(f"📊 Market Price: ${price:.2f} from {source} | "
                               f"Range: {price_info.get('low', 'N/A')}-{price_info.get('high', 'N/A')} | "
                               f"1m bar: {price_info.get('range', 0):.2f} pts")
            else:
                logging.warning("⚠️ No current market price available - data feed may be stale")
            
        except Exception as e:
            logging.error(f"Data feed monitor error: {e}")

    
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
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=45, timezone=CT),  # Changed from minute='0,15,30,45', second=30
        id='market_analysis',
        replace_existing=True
    )
    
    # Position management every 2 minutes (offset from main cron)
    scheduler.add_job(
        position_management_job,
        CronTrigger(minute='1,3,6,8,11,13,16,18,21,23,26,28,31,33,36,38,41,43,46,48,51,53,56,58', 
                   second=0, timezone=CT),
        id='position_management',
        replace_existing=True
    )

    # Data feed monitor - runs every minute during market hours
    scheduler.add_job(
        monitor_data_feed,
        CronTrigger(minute='*', second=15, timezone=CT),  # Every minute at :15 seconds
        id='data_feed_monitor',
        replace_existing=True
    )
    
    # Account health check every 30 minutes
    scheduler.add_job(
        account_health_check,
        CronTrigger(minute='0,30', second=45, timezone=CT),
        id='account_health',
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
    logging.info("[APScheduler] Scheduler started with market monitoring and position management jobs")
    
    return scheduler


def execute_autonomous_trade(trade_decision):
    """Execute an autonomous trade decision - ALWAYS goes through AI for decision ID"""
    try:
        from api import get_contract, ai_trade_decision_with_regime
        
        acct_id = ACCOUNTS.get(trade_decision['account'])
        if not acct_id:
            logging.error(f"Unknown account: {trade_decision['account']}")
            return
        
        # Get AI validation for autonomous trade
        ai_endpoints = {
            "epsilon": config['N8N_AI_URL'],
            "beta": config['N8N_AI_URL'],
        }
        
        ai_url = ai_endpoints.get(trade_decision['account'])
        if ai_url:
            # Add autonomous flag to help AI understand context
            trade_decision['autonomous'] = True
            trade_decision['initiated_by'] = 'position_manager'
            
            # CRITICAL: This goes through n8n which assigns ai_decision_id
            ai_decision = ai_trade_decision_with_regime(
                trade_decision['account'],
                trade_decision['strategy'],
                trade_decision['signal'],
                trade_decision['symbol'],
                trade_decision['size'],
                trade_decision['alert'],
                ai_url
            )
            
            # AI decision will have ai_decision_id from n8n workflow
            ai_decision_id = ai_decision.get('ai_decision_id')
            
            if ai_decision.get("signal", "").upper() not in ("BUY", "SELL"):
                logging.info(f"AI blocked autonomous trade: {ai_decision.get('reason', 'No reason')}")
                # Log the rejection
                if ai_decision_id:
                    logging.info(f"AI rejection logged with ai_decision_id: {ai_decision_id}")
                return
            
            # Execute the trade WITH the ai_decision_id
            run_bracket(
                acct_id,
                trade_decision['symbol'],
                ai_decision['signal'],
                ai_decision['size'],
                ai_decision['alert'],
                ai_decision_id  # This links the trade to the AI hypothesis
            )
            
            logging.info(f"✅ Autonomous trade executed: {ai_decision['signal']} "
                       f"{ai_decision['size']} contracts - ai_decision_id: {ai_decision_id}")
        else:
            logging.error(f"No AI endpoint for account {trade_decision['account']}")
            
    except Exception as e:
        logging.error(f"Error executing autonomous trade: {e}")
