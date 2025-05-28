# api.py
import requests
import logging
import json
import time
import pytz
import threading
import hashlib

from datetime import datetime, timezone, timedelta
from auth import ensure_token, get_token
from config import load_config
from dateutil import parser
from market_regime import MarketRegime
from supabase import create_client, Client
from typing import Dict, List, Optional, Tuple

session = requests.Session()
config = load_config()
ACCOUNTS = config['ACCOUNTS'] 
OVERRIDE_CONTRACT_ID = config['OVERRIDE_CONTRACT_ID']
PX_BASE = config['PX_BASE']
SUPABASE_URL = config['SUPABASE_URL']
SUPABASE_KEY = config['SUPABASE_KEY']
CT = pytz.timezone("America/Chicago")

# Global lock for regime cache updates
regime_cache_lock = threading.Lock()
regime_cache_in_progress = {}


# ─── API Functions ────────────────────────────────────
def post(path, payload):
    ensure_token()
    url = f"{PX_BASE}{path}"
    logging.debug("POST %s payload=%s", url, payload)
    resp = session.post(
        url,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {get_token()}"
        },
        timeout=(3.05, 10)
    )
    if resp.status_code == 429:
        logging.warning("Rate limit hit: %s %s", resp.status_code, resp.text)
    if resp.status_code >= 400:
        logging.error("Error on POST %s: %s", url, resp.text)
    resp.raise_for_status()
    data = resp.json()
    logging.debug("Response JSON: %s", data)
    return data


#Parameters -- /api/Order/place
#Name	Type	Description	Required	Nullable
#accountId	integer	The account ID.	Required	false
#contractId	string	The contract ID.	Required	false
#type	integer	The order type:
#1 = Limit
#2 = Market
#4 = Stop
#5 = TrailingStop
#6 = JoinBid
#7 = JoinAsk	Required	false
#side	integer	The side of the order:
#0 = Bid (buy)
#1 = Ask (sell)	Required	false
#size	integer	The size of the order.	Required	false
#limitPrice	decimal	The limit price for the order, if applicable.	Optional	true
#stopPrice	decimal	The stop price for the order, if applicable.	Optional	true
#trailPrice	decimal	The trail price for the order, if applicable.	Optional	true
#customTag	string	An optional custom tag for the order.	Optional	true
#linkedOrderId	integer	The linked order id.	Optional	true


def place_market(acct_id, cid, side, size):
    logging.info("Placing market order acct=%s cid=%s side=%s size=%s", acct_id, cid, side, size)
    return post("/api/Order/place", {
        "accountId": acct_id, "contractId": cid,
        "type": 2, "side": side, "size": size
    })

def place_limit(acct_id, cid, side, size, px):
    logging.info("Placing limit order acct=%s cid=%s size=%s px=%s", acct_id, cid, size, px)
    return post("/api/Order/place", {
        "accountId": acct_id, "contractId": cid,
        "type": 1, "side": side, "size": size, "limitPrice": px
    })

def place_stop(acct_id, cid, side, size, px):
    logging.info("Placing stop order acct=%s cid=%s size=%s px=%s", acct_id, cid, size, px)
    return post("/api/Order/place", {
        "accountId": acct_id, "contractId": cid,
        "type": 4, "side": side, "size": size, "stopPrice": px
    })

# example of /api/Order/searchOpen return data
#{
#    "orders": [
#        {
#            "id": 26970,
#            "accountId": 212,
#            "contractId": "CON.F.US.EP.M25",
#            "creationTimestamp": "2025-04-21T19:45:52.105808+00:00",
#            "updateTimestamp": "2025-04-21T19:45:52.105808+00:00",
#            "status": 1,
#            "type": 4,
#            "side": 1,
#            "size": 1,
#            "limitPrice": null,
#            "stopPrice": 5138.000000000
#        }
#    ],
#    "success": true,
#    "errorCode": 0,
#    "errorMessage": null
#}
def search_open(acct_id):
    orders = post("/api/Order/searchOpen", {"accountId": acct_id}).get("orders", [])
    logging.debug("Open orders for %s: %s", acct_id, orders)
    return orders

#requires accountid and orderid
def cancel(acct_id, order_id):
    resp = post("/api/Order/cancel", {"accountId": acct_id, "orderId": order_id})
    if not resp.get("success", True):
        logging.warning("Cancel reported failure: %s", resp)
    return resp

#example of /api/Position/searchOpen return data
#{
#    "positions": [
#        {
#            "id": 6124,
#            "accountId": 536,
#            "contractId": "CON.F.US.GMET.J25",
#            "creationTimestamp": "2025-04-21T19:52:32.175721+00:00",
#            "type": 1,
#            "size": 2,
#            "averagePrice": 1575.750000000
#        }
#    ],
#    "success": true,
#    "errorCode": 0,
#    "errorMessage": null
#}
def search_pos(acct_id):
    pos = post("/api/Position/searchOpen", {"accountId": acct_id}).get("positions", [])
    logging.debug("Open positions for %s: %s", acct_id, pos)
    return pos




#requires accountid and contractid
def close_pos(acct_id, cid):
    resp = post("/api/Position/closeContract", {"accountId": acct_id, "contractId": cid})
    if not resp.get("success", True):
        logging.warning("Close position reported failure: %s", resp)
    return resp

#requires accountid and starttimestamp, optional endtimestamp
#example of /api/trade/search return data
#{
#    "trades": [
#        {
#            "id": 8604,
#            "accountId": 203,
#            "contractId": "CON.F.US.EP.H25",
#            "creationTimestamp": "2025-01-21T16:13:52.523293+00:00",
#            "price": 6065.250000000,
#            "profitAndLoss": 50.000000000,
#            "fees": 1.4000,
#            "side": 1,
#            "size": 1,
#            "voided": false,
#            "orderId": 14328
#        },
#        {
#            "id": 8603,
#            "accountId": 203,
#            "contractId": "CON.F.US.EP.H25",
#            "creationTimestamp": "2025-01-21T16:13:04.142302+00:00",
#            "price": 6064.250000000,
#            "profitAndLoss": null,    //a null value indicates a half-turn trade
#            "fees": 1.4000,
#            "side": 0,
#            "size": 1,
#            "voided": false,
#            "orderId": 14326
#        }
#    ],
#    "success": true,
#    "errorCode": 0,
#    "errorMessage": null
#}
def search_trades(acct_id, since):
    trades = post("/api/Trade/search", {"accountId": acct_id, "startTimestamp": since.isoformat()}).get("trades", [])
    return trades

#custom flatten function
def flatten_contract(acct_id, cid, timeout=10):
    logging.info("Flattening contract %s for acct %s", cid, acct_id)
    end = time.time() + timeout
    while time.time() < end:
        open_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]
        if not open_orders:
            break
        for o in open_orders:
            try:
                cancel(acct_id, o["id"])
            except Exception as e:
                logging.error("Error cancelling %s: %s", o["id"], e)
        time.sleep(1)
    while time.time() < end:
        positions = [p for p in search_pos(acct_id) if p["contractId"] == cid]
        if not positions:
            break
        for _ in positions:
            try:
                close_pos(acct_id, cid)
            except Exception as e:
                logging.error("Error closing position %s: %s", cid, e)
        time.sleep(1)
    while time.time() < end:
        rem_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]
        rem_pos    = [p for p in search_pos(acct_id) if p["contractId"] == cid]
        if not rem_orders and not rem_pos:
            logging.info("Flatten complete for %s", cid)
            return True
        logging.info("Waiting for flatten: %d orders, %d positions remain", len(rem_orders), len(rem_pos))
        time.sleep(1)
    logging.error("Flatten timeout: %s still has %d orders, %d positions", cid, len(rem_orders), len(rem_pos))
    return False

#custom cancel stops function
def cancel_all_stops(acct_id, cid):
    for o in search_open(acct_id):
        if o["contractId"] == cid and o["type"] == 4:
            cancel(acct_id, o["id"])

def get_contract(sym):
    if OVERRIDE_CONTRACT_ID:
        return OVERRIDE_CONTRACT_ID
    return None

def ai_trade_decision(account, strat, sig, sym, size, alert, ai_url):
    payload = {
        "account": account,
        "strategy": strat,
        "signal": sig,
        "symbol": sym,
        "size": size,
        "alert": alert
    }
    try:
        resp = session.post(ai_url, json=payload, timeout=240)
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception as e:
            logging.error(f"AI response not valid JSON: {resp.text}")
            return {
                "strategy": strat,
                "signal": "HOLD",
                "account": account,
                "reason": f"AI response not valid JSON: {str(e)} (raw: {resp.text})",
                "error": True
            }
        if not data:
            logging.error(f"AI response empty or null: {resp.text}")
            return {
                "strategy": strat,
                "signal": "HOLD",
                "account": account,
                "reason": "AI response was empty.",
                "error": True
            }
        return data
    except Exception as e:
        logging.error(f"AI error: {str(e)}")
        return {
            "strategy": strat,
            "signal": "HOLD",
            "account": account,
            "reason": f"AI error: {str(e)}",
            "error": True
        }

#custom phantom order function
def check_for_phantom_orders(acct_id, cid):
  
    # 1. Check for open position(s)
    positions = [p for p in search_pos(acct_id) if p["contractId"] == cid]
    open_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]

    # 2. If there is an open position, make sure there are protective orders
    if positions:
        has_protective = any(o["type"] in (1, 4) and o["status"] == 1 for o in open_orders)
        if not has_protective:
            logging.warning(f"Phantom position detected! No stop/limit attached. Positions: {positions}, Orders: {open_orders}")
            flatten_contract(acct_id, cid, timeout=10)
    else:
        # 3. If there are no positions, but open stop/limit orders remain, cancel them
        leftover_orders = [o for o in open_orders if o["type"] in (1, 4) and o["status"] == 1]
        if leftover_orders:
            logging.warning(f"Leftover stop/limit order(s) found without a position! Orders: {leftover_orders}")
            for o in leftover_orders:
                try:
                    cancel(acct_id, o["id"])
                except Exception as e:
                    logging.error(f"Error cancelling phantom order {o['id']}: {e}")



#log results to supabase, important to collect results and compare against ai decision logic/hypothesis
def log_trade_results_to_supabase(acct_id, cid, entry_time, ai_decision_id, meta=None):
    import json
    import time
    from datetime import datetime, timedelta
    import logging

    meta = meta or {}

    # --- Normalize entry_time to be timezone-aware (Chicago) ---
    try:
        if isinstance(entry_time, (float, int)):
            entry_time = datetime.fromtimestamp(entry_time, CT)
        elif isinstance(entry_time, str):
            entry_time = parser.isoparse(entry_time).astimezone(CT)
        elif entry_time.tzinfo is None:
            entry_time = CT.localize(entry_time)
        else:
            entry_time = entry_time.astimezone(CT)
    except Exception as e:
        logging.error(f"[log_trade_results_to_supabase] entry_time conversion error: {entry_time} ({type(entry_time)}): {e}")
        entry_time = datetime.now(CT)

    exit_time = datetime.now(CT)
    
    # IMPORTANT: Only get trades that are part of THIS trading session
    # We need to be more precise about which trades belong to this position
    
    # First, let's get the entry order ID if we have it
    entry_order_id = meta.get("order_id")
    
    # Get trades from a reasonable window
    start_time = entry_time - timedelta(seconds=30)  # Reduced from 2 minutes to 30 seconds
    
    try:
        resp = post("/api/Trade/search", {
            "accountId": acct_id,
            "startTimestamp": start_time.isoformat()
        })
        all_trades = resp.get("trades", [])

        # Filter for this contract and not voided
        contract_trades = [
            t for t in all_trades
            if t.get("contractId") == cid and not t.get("voided", False)
        ]

        # Now we need to identify which trades belong to THIS position session
        relevant_trades = []
        
        if entry_order_id:
            # Find the entry trade
            entry_trade = None
            for t in contract_trades:
                if t.get("orderId") == entry_order_id:
                    entry_trade = t
                    break
            
            if entry_trade:
                entry_trade_time = parser.isoparse(entry_trade["creationTimestamp"])
                
                # Get all trades from entry until now that have P&L (exit trades)
                for t in contract_trades:
                    trade_time = parser.isoparse(t["creationTimestamp"])
                    # Include the entry trade and any exit trades after it
                    if t == entry_trade or (trade_time > entry_trade_time and t.get("profitAndLoss") is not None):
                        relevant_trades.append(t)
            else:
                # Fallback: use time-based approach
                logging.warning(f"Entry trade not found for order_id {entry_order_id}, using time-based approach")
                relevant_trades = get_trades_by_time_window(contract_trades, entry_time, exit_time)
        else:
            # No entry order ID, use time-based approach
            relevant_trades = get_trades_by_time_window(contract_trades, entry_time, exit_time)

        if not relevant_trades:
            logging.warning("[log_trade_results_to_supabase] No relevant trades found, skipping Supabase log.")
            return

        # Calculate P&L only from exit trades (trades with profitAndLoss)
        exit_trades = [t for t in relevant_trades if t.get("profitAndLoss") is not None]
        total_pnl = sum(float(t.get("profitAndLoss", 0)) for t in exit_trades)
        
        # Get unique trade IDs
        trade_ids = [t.get("id") for t in relevant_trades if t.get("id")]
        
        # Calculate actual duration based on first and last trade
        if relevant_trades:
            first_trade_time = min(parser.isoparse(t["creationTimestamp"]) for t in relevant_trades)
            last_trade_time = max(parser.isoparse(t["creationTimestamp"]) for t in relevant_trades)
            duration_sec = int((last_trade_time - first_trade_time).total_seconds())
        else:
            duration_sec = int((exit_time - entry_time).total_seconds())

        ai_decision_id_out = str(ai_decision_id) if ai_decision_id is not None else None

        payload = {
            "strategy":      str(meta.get("strategy") or ""),
            "signal":        str(meta.get("signal") or ""),
            "symbol":        str(meta.get("symbol") or ""),
            "account":       str(meta.get("account") or ""),
            "size":          int(meta.get("size") or 0),
            "ai_decision_id": ai_decision_id_out,
            "entry_time":    entry_time.isoformat(),
            "exit_time":     exit_time.isoformat(),
            "duration_sec":  str(duration_sec),
            "alert":         str(meta.get("alert") or ""),
            "total_pnl":     float(total_pnl),
            "raw_trades":    relevant_trades,
            "order_id":      str(meta.get("order_id") or ""),
            "comment":       f"Entry order: {entry_order_id}, Exit trades: {len(exit_trades)}",
            "trade_ids":     trade_ids,
        }

        # Log to Supabase
        url = f"{SUPABASE_URL}/rest/v1/trade_results"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
        
        r = session.post(url, json=payload, headers=headers, timeout=(3.05, 10))
        if r.status_code == 201:
            logging.info(f"[log_trade_results_to_supabase] Uploaded trade result: "
                        f"acct={acct_id}, trades={len(relevant_trades)}, "
                        f"PnL={total_pnl:.2f}, ai_id={ai_decision_id_out}")
        else:
            logging.warning(f"[log_trade_results_to_supabase] Supabase returned {r.status_code}: {r.text}")
        r.raise_for_status()

    except Exception as e:
        logging.error(f"[log_trade_results_to_supabase] Error: {e}")


def get_trades_by_time_window(contract_trades, entry_time, exit_time):
    """
    Helper function to get trades within a time window when we don't have order IDs.
    This tries to be smarter about grouping trades that belong together.
    """
    relevant_trades = []
    
    # Group trades into sessions based on time gaps
    if not contract_trades:
        return []
    
    # Sort trades by time
    sorted_trades = sorted(contract_trades, key=lambda t: t.get("creationTimestamp", ""))
    
    # Find trade sessions (groups of trades with small time gaps between them)
    sessions = []
    current_session = []
    
    for i, trade in enumerate(sorted_trades):
        if i == 0:
            current_session.append(trade)
        else:
            prev_time = parser.isoparse(sorted_trades[i-1]["creationTimestamp"])
            curr_time = parser.isoparse(trade["creationTimestamp"])
            
            # If more than 5 minutes between trades, consider it a new session
            if (curr_time - prev_time).total_seconds() > 300:
                if current_session:
                    sessions.append(current_session)
                current_session = [trade]
            else:
                current_session.append(trade)
    
    if current_session:
        sessions.append(current_session)
    
    # Find the session that best matches our entry time
    best_session = None
    best_time_diff = float('inf')
    
    for session in sessions:
        session_start = parser.isoparse(session[0]["creationTimestamp"])
        time_diff = abs((session_start - entry_time).total_seconds())
        
        if time_diff < best_time_diff and time_diff < 60:  # Within 1 minute
            best_session = session
            best_time_diff = time_diff
    
    return best_session or []



def get_supabase_client() -> Client:
    url = SUPABASE_URL
    key = SUPABASE_KEY
    supabase: Client = create_client(url, key)
    return supabase



#regime updates

# Create global market regime analyzer
market_regime_analyzer = MarketRegime()


class RegimeTracker:
    def __init__(self):
        self.last_regime = None
        self.regime_start_time = None
        
    def check_regime_change(self, new_regime: str, confidence: int) -> Optional[Dict]:
        """Check if regime has changed significantly"""
        if self.last_regime is None:
            self.last_regime = new_regime
            self.regime_start_time = datetime.now(CT)
            return None
            
        if new_regime != self.last_regime and confidence > 70:
            duration = (datetime.now(CT) - self.regime_start_time).total_seconds() / 60
            change_info = {
                'from': self.last_regime,
                'to': new_regime,
                'duration_minutes': duration,
                'timestamp': datetime.now(CT).isoformat()
            }
            
            self.last_regime = new_regime
            self.regime_start_time = datetime.now(CT)
            
            # Log significant changes
            logging.warning(f"🔄 REGIME CHANGE: {change_info['from']} → {change_info['to']} "
                          f"(lasted {duration:.1f} minutes)")
            
            return change_info
            
        return None


def fetch_multi_timeframe_analysis(n8n_base_url: str, timeframes: List[str] = None, 
                                 cache_minutes: int = 4, force_refresh: bool = False) -> Dict:
    """
    Fetch multi-timeframe analysis - NOW USING OHLC REGIME DETECTION
    """
    import datetime
    import concurrent.futures
    
    now = datetime.datetime.now(datetime.timezone.utc)
    supabase_client = get_supabase_client()
    
    if timeframes is None:
        timeframes = ['5m', '15m', '30m']
    
    # Create a unique key for this analysis request
    request_key = hashlib.md5(f"{n8n_base_url}:{','.join(timeframes)}:{force_refresh}".encode()).hexdigest()
    
    # Check if another thread is already fetching this exact analysis
    with regime_cache_lock:
        if request_key in regime_cache_in_progress:
            # Wait for the other thread to complete (with timeout)
            start_wait = time.time()
            while request_key in regime_cache_in_progress and (time.time() - start_wait) < 30:
                time.sleep(0.1)
            
            # Try to get the result from cache now
            if not force_refresh:
                try:
                    recent = supabase_client.table('market_regime_cache') \
                        .select('*') \
                        .order('timestamp', desc=True) \
                        .limit(1) \
                        .execute()
                    
                    if recent.data:
                        rec = recent.data[0]
                        timestamp_str = rec.get('timestamp')
                        if timestamp_str:
                            timestamp = parser.parse(timestamp_str)
                            age_seconds = (now - timestamp).total_seconds()
                            if age_seconds < 60:  # If less than 1 minute old, use it
                                logging.info("Using very recent cached regime analysis")
                                cached_data = json.loads(rec['analysis_data'])
                                if isinstance(cached_data, dict) and 'regime_analysis' in cached_data:
                                    return cached_data
                except Exception as e:
                    logging.warning(f"Error checking recent cache: {e}")
        
        # Mark that we're fetching this analysis
        regime_cache_in_progress[request_key] = True
    
    try:
        logging.info(f"[fetch_multi_timeframe_analysis] Called at {now}, cache_minutes={cache_minutes}, "
                    f"force_refresh={force_refresh}, request_key={request_key[:8]}, "
                    f"thread={threading.current_thread().name}")
        
        # Check cache first (unless force_refresh)
        if not force_refresh:
            try:
                # Use a more specific query to avoid duplicates
                recent = supabase_client.table('market_regime_cache') \
                    .select('*') \
                    .gte('timestamp', (now - datetime.timedelta(minutes=cache_minutes)).isoformat()) \
                    .order('timestamp', desc=True) \
                    .limit(1) \
                    .execute()
                
                if recent.data:
                    rec = recent.data[0]
                    logging.info("Using cached regime analysis from Supabase")
                    cached_data = json.loads(rec['analysis_data'])
                    
                    # Validate cached data structure
                    if isinstance(cached_data, dict) and 'regime_analysis' in cached_data:
                        return cached_data
                    else:
                        logging.warning("Invalid cached data structure, fetching fresh data")
            except Exception as e:
                logging.info(f"Cache retrieval failed: {e}")
        
        # ===== NEW: Try OHLC regime detection first =====
        try:
            logging.info("Attempting OHLC-based regime detection...")
            
            # Fetch OHLC data for all timeframes
            ohlc_data = {}
            missing_timeframes = []
            
            for tf in timeframes:
                tf_minutes = int(tf.replace('m', ''))
                
                # Get last 50 bars for each timeframe
                result = supabase_client.table('tv_datafeed') \
                    .select('*') \
                    .eq('symbol', 'MES') \
                    .eq('timeframe', 1) \
                    .order('ts', desc=True) \
                    .limit(200) \
                    .execute()
                
                if result.data and len(result.data) >= 20:  # Need minimum 20 bars
                    # Reverse to chronological order
                    data = list(reversed(result.data))
                    
                    # Extract OHLC + indicators
                    ohlc_data[tf] = {
                        'open': [float(d['o']) for d in data],
                        'high': [float(d['h']) for d in data],
                        'low': [float(d['l']) for d in data],
                        'close': [float(d['c']) for d in data],
                        'volume': [float(d.get('v', 0)) for d in data],
                        # Include all available indicators
                        'rsi': [float(d.get('rsi', 50)) for d in data],
                        'macd_hist': [float(d.get('macd_hist', 0)) for d in data],
                        'atr': [float(d.get('atr', 10)) for d in data],
                        'fisher': [float(d.get('fisher', 0)) for d in data],
                        'vzo': [float(d.get('vzo', 0)) for d in data],
                        'phobos': [float(d.get('phobos_momentum', 0)) for d in data],
                        'stoch_k': [float(d.get('stoch_k', 50)) for d in data],
                        'bb_upper': [float(d.get('bb_upper', 0)) for d in data],
                        'bb_middle': [float(d.get('bb_middle', 0)) for d in data],
                        'bb_lower': [float(d.get('bb_lower', 0)) for d in data]
                    }
                    logging.info(f"Loaded {len(data)} bars of {tf} OHLC data")
                else:
                    missing_timeframes.append(tf)
                    logging.warning(f"Insufficient OHLC data for {tf} (got {len(result.data) if result.data else 0} bars)")
            
            # If we have OHLC data for all timeframes, use it
            if len(ohlc_data) == len(timeframes):
                from market_regime_ohlc import OHLCRegimeDetector
                ohlc_detector = OHLCRegimeDetector()
                
                # Get OHLC-based regime
                regime_analysis = ohlc_detector.analyze_regime(ohlc_data)
                
                # Get the latest chart URLs for reference (but not for analysis)
                chart_urls = {}
                for tf in timeframes:
                    try:
                        chart_result = supabase_client.table('latest_chart_analysis') \
                            .select('snapshot') \
                            .eq('symbol', 'MES') \
                            .eq('timeframe', tf) \
                            .order('timestamp', desc=True) \
                            .limit(1) \
                            .execute()
                        
                        if chart_result.data:
                            snapshot = chart_result.data[0].get('snapshot', {})
                            if isinstance(snapshot, str):
                                snapshot = json.loads(snapshot)
                            chart_urls[tf] = {
                                "url": snapshot.get("url"),
                                "chart_time": snapshot.get("chart_time")
                            }
                    except Exception as e:
                        logging.debug(f"Could not get chart URL for {tf}: {e}")
                
                # Build complete response
                snapshot = {
                    'timeframe_data': {
                        tf: regime_analysis['timeframe_analysis'][tf] 
                        for tf in timeframes 
                        if tf in regime_analysis.get('timeframe_analysis', {})
                    },
                    'regime_analysis': regime_analysis,
                    'chart_urls': chart_urls,
                    'timestamp': now.isoformat(),
                    'analysis_method': 'ohlc'  # Track which method was used
                }
                
                logging.info(f"OHLC regime detection successful: {regime_analysis['primary_regime']} "
                           f"(confidence: {regime_analysis['confidence']}%)")
                
                # Save regime analysis to cache
                try:
                    # First, delete any entries from the last second to prevent near-duplicates
                    one_second_ago = (now - datetime.timedelta(seconds=1)).isoformat()
                    supabase_client.table('market_regime_cache') \
                        .delete() \
                        .gte('timestamp', one_second_ago) \
                        .execute()
                    
                    # Now insert the new entry
                    supabase_client.table('market_regime_cache').insert({
                        'analysis_data': json.dumps(snapshot),
                        'timestamp': now.isoformat()
                    }).execute()
                    logging.info("Saved OHLC regime analysis to cache")
                except Exception as e:
                    logging.error(f"Failed to save regime cache: {e}")
                
                return snapshot
            else:
                logging.warning(f"Missing OHLC data for timeframes: {missing_timeframes}, falling back to image analysis")
                
        except Exception as e:
            logging.error(f"OHLC regime detection failed, falling back to image analysis: {e}", exc_info=True)
        
        # ===== FALLBACK: Original image-based analysis =====
        logging.info("Using image-based regime detection...")
        
        timeframe_data = {}
        chart_urls = {}
        
        # Step 2: Make concurrent n8n calls for timeframes
        logging.info(f"Fetching fresh analysis from n8n for: {timeframes}")
        
        def fetch_timeframe(tf):
            """Fetch a single timeframe from n8n"""
            try:
                webhook_url = f"{n8n_base_url}/webhook/{tf}"
                response = session.post(webhook_url, json={}, timeout=60)
                response.raise_for_status()
                
                if response.text.strip():
                    try:
                        data = response.json()
                        
                        # Handle different response formats
                        if isinstance(data, str):
                            data = json.loads(data)
                        elif isinstance(data, list):
                            # n8n might return array of items, take first
                            if data:
                                data = data[0]
                            else:
                                logging.error(f"Empty array response from {tf} webhook")
                                return tf, {}
                        
                        logging.info(f"Successfully fetched {tf} analysis")
                        return tf, data
                    except json.JSONDecodeError as e:
                        logging.error(f"Failed to parse {tf} JSON response: {e}")
                        return tf, {}
                else:
                    logging.error(f"Empty response from {tf} webhook")
                    return tf, {}
                    
            except Exception as e:
                logging.error(f"Failed to fetch {tf} analysis: {e}")
                return tf, {}
        
        # Make all n8n calls concurrently
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_tf = {executor.submit(fetch_timeframe, tf): tf for tf in timeframes}
            
            for future in concurrent.futures.as_completed(future_to_tf):
                tf, data = future.result()
                if data:
                    timeframe_data[tf] = data
                    # Collect URL for archival
                    chart_urls[tf] = {
                        "url": data.get("url"),
                        "chart_time": data.get("chart_time")
                    }

        # Analyze regime with whatever data we have
        logging.info(f"Analyzing regime with {len(timeframe_data)} timeframes of data")
        regime_analysis = market_regime_analyzer.analyze_regime(timeframe_data)
        
        # Create combined snapshot with URLs
        snapshot = {
            'timeframe_data': timeframe_data,
            'regime_analysis': regime_analysis,
            'chart_urls': chart_urls,
            'timestamp': now.isoformat(),
            'analysis_method': 'image'  # Track which method was used
        }

        # Save regime analysis to cache
        try:
            # First, delete any entries from the last second to prevent near-duplicates
            one_second_ago = (now - datetime.timedelta(seconds=1)).isoformat()
            supabase_client.table('market_regime_cache') \
                .delete() \
                .gte('timestamp', one_second_ago) \
                .execute()
            
            # Now insert the new entry
            supabase_client.table('market_regime_cache').insert({
                'analysis_data': json.dumps(snapshot),
                'timestamp': now.isoformat()
            }).execute()
            logging.info("Saved image-based regime analysis to cache")
        except Exception as e:
            logging.error(f"Failed to save regime cache: {e}")

        return snapshot
        
    finally:
        # Always clean up the in-progress marker
        with regime_cache_lock:
            regime_cache_in_progress.pop(request_key, None)


def ai_trade_decision_with_regime(account, strat, sig, sym, size, alert, ai_url):
    """
    Enhanced AI trade decision that includes market regime analysis, chart URLs, and position context
    """
    try:
        # Extract base URL properly
        if '/webhook/' in ai_url:
            n8n_base_url = ai_url.split('/webhook/')[0]
        else:
            # Fallback: assume the URL structure
            n8n_base_url = ai_url.replace('/webhook', '')
        
        # Get market regime analysis with chart URLs
        market_analysis = fetch_multi_timeframe_analysis(n8n_base_url, force_refresh=False)
        
        regime = market_analysis['regime_analysis']
        regime_rules = market_regime_analyzer.get_regime_trading_rules(regime['primary_regime'])
        chart_urls = market_analysis.get('chart_urls', {})
        
        # Get account ID for position context
        acct_id = ACCOUNTS.get(account.lower())
        if acct_id:
            # Get position context
            from position_manager import PositionManager
            pm = PositionManager(ACCOUNTS)
            cid = get_contract(sym)
            position_context = pm.get_position_context_for_ai(acct_id, cid)
            
            # Check if account can trade
            if not position_context['account_metrics']['can_trade']:
                logging.warning(f"Account {account} cannot trade due to risk limits")
                return {
                    "strategy": strat,
                    "signal": "HOLD",
                    "account": account,
                    "reason": f"Account risk limits exceeded: {', '.join(position_context['warnings'])}",
                    "regime": regime['primary_regime'],
                    "error": False
                }
        else:
            position_context = None
        
        # Check if trading is recommended in this regime
        if not regime['trade_recommendation']:
            logging.warning(f"Trading not recommended in {regime['primary_regime']} regime. Blocking trade.")
            return {
                "strategy": strat,
                "signal": "HOLD",
                "account": account,
                "reason": f"Market regime ({regime['primary_regime']}) not suitable for trading. {', '.join(regime['supporting_factors'])}",
                "regime": regime['primary_regime'],
                "error": False
            }
        
        # Check if signal aligns with regime
        if regime_rules['avoid_signal'] == 'BOTH' or (regime_rules['avoid_signal'] and sig == regime_rules['avoid_signal']):
            logging.warning(f"Signal {sig} conflicts with {regime['primary_regime']} regime preferences")
            return {
                "strategy": strat,
                "signal": "HOLD",
                "account": account,
                "reason": f"{sig} signal not recommended in {regime['primary_regime']} regime",
                "regime": regime['primary_regime'],
                "error": False
            }
        
        # Prepare enhanced payload for AI with chart URLs for archival and position context
        payload = {
            "account": account,
            "strategy": strat,
            "signal": sig,
            "symbol": sym,
            "size": size,
            "alert": alert,
            "market_analysis": {
                "regime": regime['primary_regime'],
                "confidence": regime['confidence'],
                "supporting_factors": regime['supporting_factors'],
                "risk_level": regime['risk_level'],
                "trend_details": regime['trend_details'],
                "volatility_details": regime['volatility_details'],
                "momentum_details": regime['momentum_details']
            },
            "regime_rules": regime_rules,
            "timeframe_signals": {
                tf: data.get('signal', 'HOLD') 
                for tf, data in market_analysis['timeframe_data'].items()
            },
            "chart_urls": chart_urls,  # Include chart URLs for archival
            "support": {
                tf: data.get('support', []) 
                for tf, data in market_analysis['timeframe_data'].items()
            },
            "resistance": {
                tf: data.get('resistance', []) 
                for tf, data in market_analysis['timeframe_data'].items()
            }
        }
        
        # Add position context if available
        if position_context:
            payload["position_context"] = position_context
            
            # Add specific position-aware rules
            if position_context['current_position']['has_position']:
                current_side = position_context['current_position']['side']
                
                # Warn about counter-trend trades
                if (current_side == 'LONG' and sig == 'SELL') or (current_side == 'SHORT' and sig == 'BUY'):
                    payload['position_warning'] = f"Signal would reverse current {current_side} position"
                
                # Suggest position size based on current exposure
                if position_context['current_position']['size'] >= 3:
                    payload['suggested_size'] = 0  # No new positions
                elif position_context['account_metrics']['risk_level'] == 'high':
                    payload['suggested_size'] = 1  # Minimum size
                else:
                    payload['suggested_size'] = size
        
        # Add autonomous trade flag if present
        if hasattr(strat, 'get') and strat.get('autonomous'):
            payload['autonomous'] = True
            payload['initiated_by'] = strat.get('initiated_by', 'unknown')
        
        resp = session.post(ai_url, json=payload, timeout=240)
        resp.raise_for_status()
        
        try:
            data = resp.json()
        except Exception as e:
            logging.error(f"AI response not valid JSON: {resp.text}")
            return {
                "strategy": strat,
                "signal": "HOLD",
                "account": account,
                "reason": f"AI response parsing error: {str(e)}",
                "regime": regime['primary_regime'],
                "error": True
            }
        
        # Add regime info to response
        data['regime'] = regime['primary_regime']
        data['regime_confidence'] = regime['confidence']
        
        # Apply position sizing based on regime and position context
        if 'size' in data and regime_rules['max_position_size'] > 0:
            data['size'] = min(data['size'], regime_rules['max_position_size'])
            
            # Further adjust based on position context
            if position_context and 'suggested_size' in payload:
                data['size'] = min(data['size'], payload['suggested_size'])
        
        return data
        
    except Exception as e:
        logging.error(f"AI error with regime analysis: {str(e)}")
        return {
            "strategy": strat,
            "signal": "HOLD",
            "account": account,
            "reason": f"AI error: {str(e)}",
            "regime": "unknown",
            "error": True
        }


# New endpoint function to add to api.py
def get_all_positions_summary() -> Dict:
    """
    Get a summary of all positions across all accounts
    """
    try:
        from position_manager import PositionManager
        pm = PositionManager(ACCOUNTS)
        
        summary = {
            'accounts': {},
            'total_positions': 0,
            'total_pnl': 0,
            'timestamp': datetime.now(CT).isoformat()
        }
        
        cid = get_contract("CON.F.US.MES.M25")
        
        for account_name, acct_id in ACCOUNTS.items():
            position_state = pm.get_position_state(acct_id, cid)
            account_state = pm.get_account_state(acct_id)
            
            summary['accounts'][account_name] = {
                'position': position_state,
                'account_metrics': account_state
            }
            
            if position_state['has_position']:
                summary['total_positions'] += 1
                summary['total_pnl'] += position_state['current_pnl']
        
        return summary
        
    except Exception as e:
        logging.error(f"Error getting positions summary: {e}")
        return {
            'error': str(e),
            'accounts': {},
            'total_positions': 0,
            'total_pnl': 0
        }

def get_market_conditions_summary(force_refresh: bool = False) -> Dict:
    """
    Get a summary of current market conditions for logging
    """
    try:
        # Extract base URL from config
        n8n_ai_url = config.get('N8N_AI_URL', '')
        if '/webhook/' in n8n_ai_url:
            n8n_base_url = n8n_ai_url.split('/webhook/')[0]
        else:
            n8n_base_url = n8n_ai_url.replace('/webhook', '')
        
        market_analysis = fetch_multi_timeframe_analysis(n8n_base_url, force_refresh=force_refresh)
        
        # Handle the case where market_analysis might be a list or have unexpected structure
        if isinstance(market_analysis, list):
            # If it's a list, try to get the first item
            if market_analysis:
                market_analysis = market_analysis[0]
            else:
                raise ValueError("Empty market analysis list")
                
        # Ensure we have a dict
        if not isinstance(market_analysis, dict):
            raise ValueError(f"Unexpected market_analysis type: {type(market_analysis)}")
        
        # Get regime analysis, handle missing key
        regime = market_analysis.get('regime_analysis', {})
        if not regime:
            raise ValueError("No regime_analysis in market data")
        
        # Handle missing trend_details gracefully
        trend_details = regime.get('trend_details', {})
        volatility_details = regime.get('volatility_details', {})
        
        summary = {
            'timestamp': datetime.now(CT).isoformat(),
            'regime': regime.get('primary_regime', 'unknown'),
            'confidence': regime.get('confidence', 0),
            'trade_recommended': regime.get('trade_recommendation', False),
            'risk_level': regime.get('risk_level', 'high'),
            'key_factors': regime.get('supporting_factors', ['Error in analysis'])[:3],  # Top 3 factors
            'trend_alignment': trend_details.get('alignment_score', 0),
            'volatility': volatility_details.get('volatility_regime', 'unknown')
        }
        
        logging.info(f"Market Conditions: {summary}")
        return summary
        
    except Exception as e:
        logging.error(f"Error getting market conditions: {e}")
        return {
            'timestamp': datetime.now(CT).isoformat(),
            'regime': 'unknown',
            'confidence': 0,
            'trade_recommended': False,
            'risk_level': 'high',
            'key_factors': ['Error in analysis'],
            'trend_alignment': 0,
            'volatility': 'unknown'
        }

def get_current_market_price(symbol: str = "MES", max_age_seconds: int = 120) -> Tuple[Optional[float], Optional[str]]:
    """
    Get the current market price from the best available source.
    """
    try:
        supabase = get_supabase_client()
        
        # First try real-time 1-minute data feed
        try:
            result = supabase.table('tv_datafeed') \
                .select('c, ts') \
                .eq('symbol', 'MES') \
                .eq('timeframe', 1) \
                .order('ts', desc=True) \
                .limit(1) \
                .execute()
            
            if result.data and len(result.data) > 0:
                record = result.data[0]
                price = float(record.get('c'))
                
                # Check data freshness
                bar_time = parser.parse(record.get('ts'))
                current_time = datetime.now(timezone.utc)  # Fixed: use imported timezone
                age_seconds = (current_time - bar_time).total_seconds()
                
                if age_seconds <= max_age_seconds:
                    logging.debug(f"Current price from 1m feed: ${price} (age: {age_seconds:.0f}s)")
                    return price, f"1m_feed_{int(age_seconds)}s_old"
                else:
                    logging.debug(f"1m data too old: {age_seconds:.0f}s > {max_age_seconds}s")
                    
        except Exception as e:
            logging.error(f"Error querying tv_datafeed: {e}")
        
        # Fallback to 5m chart analysis
        try:
            result = supabase.table('latest_chart_analysis') \
                .select('snapshot, timestamp') \
                .eq('symbol', 'MES') \
                .eq('timeframe', '5m') \
                .order('timestamp', desc=True) \
                .limit(1) \
                .execute()
            
            if result.data and len(result.data) > 0:
                record = result.data[0]
                
                # Check age
                timestamp = parser.parse(record.get('timestamp'))
                age_seconds = (datetime.now(timezone.utc) - timestamp).total_seconds()  # Fixed
                
                if age_seconds <= 360:  # Accept up to 6 minutes old for chart data
                    snapshot = record.get('snapshot')
                    if isinstance(snapshot, str):
                        snapshot = json.loads(snapshot)
                    
                    price = snapshot.get('current_price')
                    if price:
                        logging.debug(f"Current price from 5m chart: ${price} (age: {age_seconds:.0f}s)")
                        return float(price), f"5m_chart_{int(age_seconds)}s_old"
                        
        except Exception as e:
            logging.debug(f"Could not get chart price: {e}")
        
        # Check if market is closed
        now = datetime.now(CT)
        is_market_closed = (
            now.weekday() == 5 or  # Saturday
            (now.weekday() == 6 and now.hour < 17) or  # Sunday before 5pm
            (now.weekday() == 4 and now.hour >= 16)  # Friday after 4pm
        )
        
        if is_market_closed:
            # Try to get last known price with much longer timeout
            try:
                result = supabase.table('tv_datafeed') \
                    .select('c, ts') \
                    .eq('symbol', 'MES') \
                    .eq('timeframe', 1) \
                    .order('ts', desc=True) \
                    .limit(1) \
                    .execute()
                
                if result.data:
                    record = result.data[0]
                    price = float(record.get('c'))
                    return price, "market_closed_last_known"
                    
            except:
                pass
        
        logging.warning("Could not determine current market price from any source")
        return None, None
        
    except Exception as e:
        logging.error(f"Error getting current market price: {e}")
        return None, None


def get_spread_and_mid_price(symbol: str = "MES") -> Dict[str, Optional[float]]:
    """
    Get bid, ask, spread, and mid price from the data feed.
    
    Returns:
        Dict with 'bid', 'ask', 'spread', 'mid' keys
    """
    try:
        supabase = get_supabase_client()
        
        # Get the most recent bar
        result = supabase.table('tv_datafeed') \
            .select('o, h, l, c, ts') \
            .eq('symbol', 'MES') \
            .eq('timeframe', 1) \
            .order('ts', desc=True) \
            .limit(1) \
            .execute()
        
        if result.data and len(result.data) > 0:
            bar = result.data[0]
            # For futures, we can approximate:
            # - High as resistance/ask area
            # - Low as support/bid area  
            # - Close as the last traded price
            # - Mid as (high + low) / 2
            
            high = float(bar.get('h'))
            low = float(bar.get('l'))
            close = float(bar.get('c'))
            
            return {
                'last': close,
                'high': high,
                'low': low,
                'mid': (high + low) / 2,
                'range': high - low,
                'timestamp': bar.get('ts')
            }
            
    except Exception as e:
        logging.error(f"Error getting price levels: {e}")
    
    return {
        'last': None,
        'high': None,
        'low': None,
        'mid': None,
        'range': None,
        'timestamp': None
    }

def fetch_ohlc_for_analysis(symbol: str = 'MES', cache_minutes: int = 5) -> Dict:
    """Fetch OHLC data for regime analysis"""
    try:
        supabase = get_supabase_client()
        
        # Calculate how many bars we need
        bars_needed = {
            '5m': 50,   # 50 bars = ~4 hours
            '15m': 50,  # 50 bars = ~12.5 hours  
            '30m': 30   # 30 bars = ~15 hours
        }
        
        ohlc_data = {}
        
        for timeframe, bars in bars_needed.items():
            # Convert timeframe to minutes for query
            tf_minutes = int(timeframe.replace('m', ''))
            
            result = supabase.table('tv_datafeed') \
                .select('o, h, l, c, v, ts') \
                .eq('symbol', symbol) \
                .eq('timeframe', tf_minutes) \
                .order('ts', desc=True) \
                .limit(bars) \
                .execute()
            
            if result.data and len(result.data) > 10:  # Need minimum bars
                # Reverse to chronological order
                data = list(reversed(result.data))
                ohlc_data[timeframe] = {
                    'open': [float(d['o']) for d in data],
                    'high': [float(d['h']) for d in data],
                    'low': [float(d['l']) for d in data],
                    'close': [float(d['c']) for d in data],
                    'volume': [float(d.get('v', 0)) for d in data]
                }
        
        return ohlc_data
        
    except Exception as e:
        logging.error(f"Error fetching OHLC data: {e}")
        return {}
