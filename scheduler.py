#scheduler.py

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging
import requests
import re
import time
from config import load_config
from api import get_market_conditions_summary
from position_manager import PositionManager
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
    
    # Original 5-minute cron job - still fetches charts
    def cron_job():
        """Runs 5 seconds after each 5-min candle close to fetch fresh charts AND update regime"""
        
        # Check if we're in the get-flat window
        from auth import in_get_flat
        now = datetime.now(CT)
        
        if in_get_flat(now):
            logging.info("[APScheduler] SKIPPING chart/regime fetch - in get-flat window")
            # Don't make any n8n calls or webhook calls during flat time
            return
        
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
    
        # Then run the normal webhook (without any trade signals)
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
    def market_analysis_job():
        """Log market conditions and alerts"""
        # Skip during flat window
        from auth import in_get_flat
        if in_get_flat(datetime.now(CT)):
            logging.info("[Market Analysis] Skipping - in get-flat window")
            return
            
        try:
            from api import get_market_conditions_summary
            summary = get_market_conditions_summary(force_refresh=False)
        
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
    
    # Position monitoring job - runs every 2 minutes
    def position_monitoring_job():
        """Monitor existing positions and log alerts"""
        # Always run position monitoring, even during flat time
        try:
            logging.info("🔄 Running position monitoring...")
            
            from api import get_contract
            cid = get_contract("CON.F.US.MES.M25")
            
            for account_name, acct_id in ACCOUNTS.items():
                try:
                    # Get position state
                    position_state = position_manager.get_position_state(acct_id, cid)
                    
                    # If we have a position, log its status
                    if position_state['has_position']:
                        logging.info(f"Position Monitor - {account_name}: "
                                   f"{position_state['size']} contracts {position_state['side']}, "
                                   f"P&L: ${position_state['current_pnl']:.2f} "
                                   f"(unrealized: ${position_state.get('unrealized_pnl', 0):.2f}), "
                                   f"Duration: {position_state['duration_minutes']:.0f} min")
                        
                        # Log alerts for concerning positions
                        if position_state['current_pnl'] < -100:
                            logging.warning(f"⚠️ {account_name}: Large loss ${position_state['current_pnl']:.2f}")
                        
                        if position_state['duration_minutes'] > 120:
                            logging.warning(f"⚠️ {account_name}: Stale position ({position_state['duration_minutes']:.0f} min)")
                        
                        if len(position_state['stop_orders']) == 0:
                            logging.warning(f"⚠️ {account_name}: No stop loss detected!")
                                
                except Exception as e:
                    logging.error(f"[Position Monitor] Error for {account_name}: {e}")
                    
        except Exception as e:
            logging.error(f"[Position Monitor] General error: {e}")
    
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
                    
                    if account_state['consecutive_losses'] >= 2:
                        logging.warning(f"⚠️ Account {account_name} has {account_state['consecutive_losses']} consecutive losses")
                        
                except Exception as e:
                    logging.error(f"[Account Health] Error for {account_name}: {e}")
                    
        except Exception as e:
            logging.error(f"[Account Health] General error: {e}")
    
    # Pre-session analysis jobs
    def pre_session_analysis(session_name):
        """Run analysis before each major session"""
        # Skip if in flat window
        from auth import in_get_flat
        if in_get_flat(datetime.now(CT)):
            logging.info(f"[Pre-session Analysis] Skipping {session_name} - in get-flat window")
            return
            
        try:
            logging.info(f"🔔 Pre-{session_name} session analysis starting...")
            summary = get_market_conditions_summary(force_refresh=True)
            
            session_recommendations = {
                'LONDON': {
                    'good_regimes': ['trending_up', 'trending_down'],
                    'warning': 'High volatility expected'
                },
                'NY_MORNING': {
                    'good_regimes': ['trending_up', 'trending_down', 'breakout'],
                    'warning': 'Highest volatility - reduce position sizes'
                },
                'NY_AFTERNOON': {
                    'good_regimes': ['trending_up', 'trending_down', 'ranging'],
                    'warning': 'Watch for trend exhaustion'
                }
            }
            
            if session_name in session_recommendations:
                rec = session_recommendations[session_name]
                if summary['regime'] not in rec['good_regimes']:
                    logging.warning(f"⚠️ {session_name} session: {summary['regime']} regime may be challenging. "
                                  f"{rec['warning']}")
                else:
                    logging.info(f"✅ {session_name} session: {summary['regime']} regime looks favorable. "
                               f"{rec['warning']}")
                               
        except Exception as e:
            logging.error(f"[Pre-session Analysis] Error: {e}")

    def monitor_data_feed():
        """Monitor data feed health and log price updates"""
        try:
            from api import get_current_market_price, get_spread_and_mid_price, get_contract
        
            # Get current price
            price, source = get_current_market_price(max_age_seconds=60000)
        
            if price:
                # Get additional price info
                price_info = get_spread_and_mid_price()
            
                # Only log if there's an open position or if market is open
                has_positions = False
                for account_name, acct_id in ACCOUNTS.items():
                    positions = position_manager.get_position_state(acct_id, get_contract("CON.F.US.MES.M25"))
                    if positions['has_position']:
                        has_positions = True
                        break
            
                # Check if market is closed
                is_market_closed = "market_closed" in source
            
                if has_positions or not is_market_closed:
                    if is_market_closed:
                        logging.info(f"📊 Last Market Price (CLOSED): ${price:.2f} from {source}")
                    else:
                        logging.info(f"📊 Market Price: ${price:.2f} from {source} | "
                                   f"Range: {price_info.get('low', 'N/A')}-{price_info.get('high', 'N/A')} | "
                                   f"1m bar: {price_info.get('range', 0):.2f} pts")
            else:
                # Only warn if market should be open
                now = datetime.now(CT)
                if not (now.weekday() == 5 or (now.weekday() == 6 and now.hour < 17) or 
                       (now.weekday() == 4 and now.hour >= 16)):
                    logging.warning("⚠️ No current market price available - data feed may be stale")
            
        except Exception as e:
            logging.error(f"Data feed monitor error: {e}")

    # Metadata cleanup - runs every hour
    def metadata_cleanup_job():
        """Clean up any orphaned trade metadata"""
        try:
            from signalr_listener import cleanup_stale_metadata
            removed = cleanup_stale_metadata(max_age_hours=12)
            if removed > 0:
                logging.info(f"Cleaned up {removed} stale metadata entries")
        except Exception as e:
            logging.error(f"[Metadata Cleanup] Error: {e}")

    # GET FLAT FUNCTIONS
    def get_flat_job():
        """Automatically flatten all positions at get-flat time"""
        try:
            logging.info("⏰ GET FLAT TIME - Flattening all positions")
            
            from api import search_pos, flatten_contract, get_contract
            
            flattened_count = 0
            errors = []
            
            # Get the contract ID for MES
            cid = get_contract("CON.F.US.MES.M25")
            
            for account_name, acct_id in ACCOUNTS.items():
                try:
                    # Get all open positions
                    positions = search_pos(acct_id)
                    
                    # Find positions for our contract
                    open_positions = [p for p in positions if p["contractId"] == cid and p.get("size", 0) > 0]
                    
                    if open_positions:
                        total_size = sum(p.get("size", 0) for p in open_positions)
                        avg_price = sum(p.get("averagePrice", 0) * p.get("size", 0) for p in open_positions) / total_size if total_size > 0 else 0
                        
                        logging.warning(f"🔻 FLATTENING {account_name}: {total_size} contracts @ ${avg_price:.2f}")
                        
                        # Flatten the position
                        success = flatten_contract(acct_id, cid, timeout=15)
                        
                        if success:
                            flattened_count += 1
                            logging.info(f"✅ Successfully flattened {account_name}")
                            
                            # Log the flatten action to Supabase for tracking
                            try:
                                from api import get_supabase_client
                                supabase = get_supabase_client()
                                
                                supabase.table('ai_trading_log').insert({
                                    'strategy': 'get_flat',
                                    'signal': 'FLAT',
                                    'symbol': 'CON.F.US.MES.M25',
                                    'account': account_name,
                                    'size': 0,
                                    'timestamp': datetime.now(CT).isoformat(),
                                    'reason': 'Automatic get-flat at 3:07 PM CT',
                                    'alert': 'Scheduled get-flat window',
                                    'ai_decision_id': f'GET_FLAT_{int(time.time())}'
                                }).execute()
                            except Exception as e:
                                logging.error(f"Failed to log get-flat action: {e}")
                        else:
                            errors.append(f"{account_name}: Failed to flatten")
                            logging.error(f"❌ Failed to flatten {account_name}")
                    else:
                        logging.info(f"No open positions for {account_name}")
                        
                except Exception as e:
                    errors.append(f"{account_name}: {str(e)}")
                    logging.error(f"Error processing {account_name}: {e}")
            
            # Summary
            if flattened_count > 0:
                logging.warning(f"🏁 GET FLAT COMPLETE: Flattened {flattened_count} accounts")
            else:
                logging.info("GET FLAT: No positions to flatten")
                
            if errors:
                logging.error(f"GET FLAT ERRORS: {', '.join(errors)}")
                
        except Exception as e:
            logging.error(f"[Get Flat Job] Critical error: {e}")

    def pre_flat_warning_job():
        """Warn 5 minutes before get-flat time"""
        try:
            logging.warning("⚠️ GET FLAT WARNING: Positions will be flattened in 5 minutes (3:07 PM CT)")
            
            # Check current positions and log warning
            from api import search_pos, get_contract
            cid = get_contract("CON.F.US.MES.M25")
            
            positions_to_flatten = []
            
            for account_name, acct_id in ACCOUNTS.items():
                positions = search_pos(acct_id)
                open_positions = [p for p in positions if p["contractId"] == cid and p.get("size", 0) > 0]
                
                if open_positions:
                    total_size = sum(p.get("size", 0) for p in open_positions)
                    positions_to_flatten.append(f"{account_name}: {total_size} contracts")
            
            if positions_to_flatten:
                logging.warning(f"⚠️ Positions to be flattened at 3:07 PM: {', '.join(positions_to_flatten)}")
                
        except Exception as e:
            logging.error(f"[Pre-flat Warning] Error: {e}")


    # Schedule jobs

    scheduler.add_job(
        metadata_cleanup_job,
        CronTrigger(minute=30, timezone=CT),  # Run at :30 every hour
        id='metadata_cleanup',
        replace_existing=True
    )
    
    # Chart fetch and regime update - every 5 minutes
    scheduler.add_job(
        cron_job,
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=5, timezone=CT),
        id='5m_job',
        replace_existing=True
    )
    
    # Market analysis every 15 minutes
    scheduler.add_job(
        market_analysis_job,
        CronTrigger(minute='0,15,30,45', second=45, timezone=CT),
        id='market_analysis',
        replace_existing=True
    )
    
    # Position monitoring every 2 minutes
    scheduler.add_job(
        position_monitoring_job,
        CronTrigger(minute='1,3,6,8,11,13,16,18,21,23,26,28,31,33,36,38,41,43,46,48,51,53,56,58', 
                   second=0, timezone=CT),
        id='position_monitoring',
        replace_existing=True
    )
    
    # Data feed monitor - every minute during market hours
    scheduler.add_job(
        monitor_data_feed,
        CronTrigger(minute='*', second=15, timezone=CT),
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
    
    # GET FLAT JOBS
    # Pre-flat warning at 3:02 PM CT (5 minutes before)
    scheduler.add_job(
        pre_flat_warning_job,
        CronTrigger(hour=15, minute=2, timezone=CT),
        id='pre_flat_warning',
        replace_existing=True
    )
    
    # Get flat at 3:07 PM CT
    scheduler.add_job(
        get_flat_job,
        CronTrigger(hour=15, minute=7, timezone=CT),
        id='get_flat',
        replace_existing=True
    )
    
    
    scheduler.start()
    logging.info("[APScheduler] Scheduler started with monitoring jobs only (no autonomous trading)")
    
    return scheduler
