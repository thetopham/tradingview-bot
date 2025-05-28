#scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import logging
import requests
import time
from datetime import datetime
from config import load_config

config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']
CT = pytz.timezone("America/Chicago")
TV_PORT = config['TV_PORT']
ACCOUNTS = config['ACCOUNTS']

def start_scheduler(app):
    scheduler = BackgroundScheduler()
    
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
                            
                            # Log the flatten action (optional - add if you have Supabase)
                            # try:
                            #     from api import get_supabase_client
                            #     supabase = get_supabase_client()
                            #     
                            #     supabase.table('ai_trading_log').insert({
                            #         'strategy': 'get_flat',
                            #         'signal': 'FLAT',
                            #         'symbol': 'CON.F.US.MES.M25',
                            #         'account': account_name,
                            #         'size': 0,
                            #         'timestamp': datetime.now(CT).isoformat(),
                            #         'reason': 'Automatic get-flat at 3:07 PM CT',
                            #         'alert': 'Scheduled get-flat window',
                            #         'ai_decision_id': f'GET_FLAT_{int(time.time())}'
                            #     }).execute()
                            # except Exception as e:
                            #     logging.error(f"Failed to log get-flat action: {e}")
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
    
    # Schedule the original 5m job
    scheduler.add_job(
        cron_job,
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=5, timezone=CT),
        id='5m_job',
        replace_existing=True
    )
    
    # Schedule pre-flat warning at 3:02 PM CT (5 minutes before)
    scheduler.add_job(
        pre_flat_warning_job,
        CronTrigger(hour=15, minute=2, timezone=CT),
        id='pre_flat_warning',
        replace_existing=True
    )
    
    # Schedule automatic flatten at 3:07 PM CT
    scheduler.add_job(
        get_flat_job,
        CronTrigger(hour=15, minute=7, timezone=CT),
        id='get_flat',
        replace_existing=True
    )
    
    scheduler.start()
    logging.info("[APScheduler] Scheduler started with 5m job and automatic get-flat at 3:07 PM CT.")
    return scheduler
