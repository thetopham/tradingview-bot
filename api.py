# api.py
import requests
import logging
import json
import time
import pytz

from datetime import datetime, timezone
from auth import ensure_token, get_token
from config import load_config
from dateutil import parser
from market_regime import MarketRegime
from supabase import create_client, Client
from typing import Dict, List

session = requests.Session()
config = load_config()
OVERRIDE_CONTRACT_ID = config['OVERRIDE_CONTRACT_ID']
PX_BASE = config['PX_BASE']
SUPABASE_URL = config['SUPABASE_URL']
SUPABASE_KEY = config['SUPABASE_KEY']
CT = pytz.timezone("America/Chicago")



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

def search_open(acct_id):
    orders = post("/api/Order/searchOpen", {"accountId": acct_id}).get("orders", [])
    logging.debug("Open orders for %s: %s", acct_id, orders)
    return orders

def cancel(acct_id, order_id):
    resp = post("/api/Order/cancel", {"accountId": acct_id, "orderId": order_id})
    if not resp.get("success", True):
        logging.warning("Cancel reported failure: %s", resp)
    return resp

def search_pos(acct_id):
    pos = post("/api/Position/searchOpen", {"accountId": acct_id}).get("positions", [])
    logging.debug("Open positions for %s: %s", acct_id, pos)
    return pos

def close_pos(acct_id, cid):
    resp = post("/api/Position/closeContract", {"accountId": acct_id, "contractId": cid})
    if not resp.get("success", True):
        logging.warning("Close position reported failure: %s", resp)
    return resp

def search_trades(acct_id, since):
    trades = post("/api/Trade/search", {"accountId": acct_id, "startTimestamp": since.isoformat()}).get("trades", [])
    return trades

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
            # Handles ISO8601 strings like '2025-05-23T13:50:50.957529+00:00'
            entry_time = parser.isoparse(entry_time).astimezone(CT)
        elif entry_time.tzinfo is None:
            entry_time = CT.localize(entry_time)
        else:
            entry_time = entry_time.astimezone(CT)
    except Exception as e:
        logging.error(f"[log_trade_results_to_supabase] entry_time conversion error: {entry_time} ({type(entry_time)}): {e}")
        entry_time = datetime.now(CT)

    exit_time = datetime.now(CT)
    start_time = entry_time - timedelta(minutes=2)
    try:
        resp = post("/api/Trade/search", {
            "accountId": acct_id,
            "startTimestamp": start_time.isoformat()
        })
        trades = resp.get("trades", [])

        relevant_trades = [
            t for t in trades
            if t.get("contractId") == cid and not t.get("voided", False) and t.get("size", 0) > 0
        ]

        if not relevant_trades:
            logging.warning("[log_trade_results_to_supabase] No relevant trades found, skipping Supabase log.")
            try:
                with open("/tmp/trade_results_missing.jsonl", "a") as f:
                    f.write(json.dumps({
                        "acct_id": acct_id,
                        "cid": cid,
                        "entry_time": entry_time.isoformat(),
                        "ai_decision_id": ai_decision_id,
                        "meta": meta,
                        "all_trades": trades
                    }) + "\n")
            except Exception as e2:
                logging.error(f"[log_trade_results_to_supabase] Failed to write missing-trade log: {e2}")
            return

        total_pnl = sum(float(t.get("profitAndLoss") or 0.0) for t in relevant_trades)
        trade_ids = [t.get("id") for t in relevant_trades]
        duration_sec = int((exit_time - entry_time).total_seconds())
        ai_decision_id_out = str(ai_decision_id) if ai_decision_id is not None else None

        payload = {
            "strategy":      str(meta.get("strategy") or ""),
            "signal":        str(meta.get("signal") or ""),
            "symbol":        str(meta.get("symbol") or ""),
            "account":       str(meta.get("account") or ""),
            "size":          int(meta.get("size") or 0),
            "ai_decision_id": ai_decision_id_out,
            "entry_time":    entry_time.isoformat() if hasattr(entry_time, "isoformat") else str(entry_time),
            "exit_time":     exit_time.isoformat() if hasattr(exit_time, "isoformat") else str(exit_time),
            "duration_sec":  str(duration_sec) if duration_sec is not None else "0",
            "alert":         str(meta.get("alert") or ""),
            "total_pnl":     float(total_pnl) if total_pnl is not None else 0.0,
            "raw_trades":    relevant_trades if relevant_trades else [],
            "order_id":      str(meta.get("order_id") or ""),
            "comment":       str(meta.get("comment") or ""),
            "trade_ids":     trade_ids if trade_ids else [],
        }

        url = f"{SUPABASE_URL}/rest/v1/trade_results"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
        try:
            r = session.post(url, json=payload, headers=headers, timeout=(3.05, 10))
            if r.status_code == 201:
                logging.info(f"[log_trade_results_to_supabase] Uploaded trade result for acct={acct_id}, cid={cid}, PnL={total_pnl}, ai_decision_id={ai_decision_id_out}")
            else:
                logging.warning(f"[log_trade_results_to_supabase] Supabase returned non-201: status={r.status_code}, text={r.text}")
            r.raise_for_status()
        except Exception as e:
            logging.error(f"[log_trade_results_to_supabase] Supabase upload failed: {e}")
            try:
                with open("/tmp/trade_results_fallback.jsonl", "a") as f:
                    f.write(json.dumps(payload) + "\n")
                logging.info("[log_trade_results_to_supabase] Trade result written to local fallback log.")
            except Exception as e2:
                logging.error(f"[log_trade_results_to_supabase] Failed to write trade result to local log: {e2}")

    except Exception as e:
        logging.error(f"[log_trade_results_to_supabase] Outer error: {e}")



def get_supabase_client() -> Client:
    url = SUPABASE_URL
    key = SUPABASE_KEY
    supabase: Client = create_client(url, key)
    return supabase



#regime updates

# Create global market regime analyzer
market_regime_analyzer = MarketRegime()

def fetch_multi_timeframe_analysis(n8n_base_url: str, timeframes: List[str] = None, cache_minutes: int = 2) -> Dict:
    """
    Fetch multi-timeframe analysis, using Supabase cache if recent.
    Fixed to use proper cache table and handle n8n calls concurrently.
    """
    import datetime
    import concurrent.futures
    import threading
    
    now = datetime.datetime.now(datetime.timezone.utc)
    supabase_client = get_supabase_client()

    # Try cache first - use a separate table for regime analysis
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
                try:
                    timestamp = parser.parse(timestamp_str)
                    if (now - timestamp).total_seconds() < cache_minutes * 60:
                        logging.info("Using cached regime analysis from Supabase.")
                        return json.loads(rec['analysis_data'])
                except Exception as e:
                    logging.warning(f"Error parsing cached timestamp: {e}")
    except Exception as e:
        logging.info(f"No cache table or cache retrieval failed: {e}")

    # If no recent cache, try to get individual timeframe data from latest_chart_analysis
    if timeframes is None:
        timeframes = ['1m', '5m', '15m', '30m', '1h']
    
    timeframe_data = {}
    
    # Step 1: Check database cache for all timeframes first
    timeframes_needing_fetch = []
    
    for tf in timeframes:
        try:
            result = supabase_client.table('latest_chart_analysis') \
                .select('*') \
                .eq('symbol', 'MES') \
                .eq('timeframe', tf) \
                .order('timestamp', desc=True) \
                .limit(1) \
                .execute()
            
            if result.data:
                record = result.data[0]
                timestamp_str = record.get('timestamp')
                if timestamp_str:
                    try:
                        timestamp = parser.parse(timestamp_str)
                        # Use cached data if less than 5 minutes old
                        if (now - timestamp).total_seconds() < 5 * 60:
                            snapshot_data = record.get('snapshot')
                            if snapshot_data:
                                if isinstance(snapshot_data, str):
                                    parsed_data = json.loads(snapshot_data)
                                elif isinstance(snapshot_data, list):
                                    # Handle case where snapshot is a list - take first item
                                    parsed_data = snapshot_data[0] if snapshot_data else {}
                                else:
                                    parsed_data = snapshot_data
                                
                                # Ensure we have a dictionary
                                if isinstance(parsed_data, dict):
                                    timeframe_data[tf] = parsed_data
                                    logging.info(f"Using cached {tf} analysis from database")
                                    continue
                                else:
                                    logging.warning(f"Invalid {tf} data structure: {type(parsed_data)}, skipping cache")
                    except Exception as e:
                        logging.warning(f"Error parsing {tf} timestamp: {e}")
            
            # If we get here, we need to fetch fresh data
            timeframes_needing_fetch.append(tf)
            
        except Exception as e:
            logging.error(f"Failed to check cache for {tf}: {e}")
            timeframes_needing_fetch.append(tf)

    # Step 2: Make concurrent n8n calls for timeframes that need fresh data
    if timeframes_needing_fetch:
        logging.info(f"Fetching fresh analysis from n8n for: {timeframes_needing_fetch}")
        
        def fetch_timeframe(tf):
            """Fetch a single timeframe from n8n"""
            try:
                webhook_url = f"{n8n_base_url}/webhook/{tf}"
                response = session.post(webhook_url, json={}, timeout=60)
                response.raise_for_status()
                
                if response.text.strip():
                    try:
                        data = response.json()
                        if isinstance(data, str):
                            data = json.loads(data)
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
            future_to_tf = {executor.submit(fetch_timeframe, tf): tf for tf in timeframes_needing_fetch}
            
            for future in concurrent.futures.as_completed(future_to_tf):
                tf, data = future.result()
                if data:
                    timeframe_data[tf] = data

    # Analyze regime with whatever data we have
    regime_analysis = market_regime_analyzer.analyze_regime(timeframe_data)
    
    # Create combined snapshot
    snapshot = {
        'timeframe_data': timeframe_data,
        'regime_analysis': regime_analysis,
        'timestamp': now.isoformat()
    }

    # Save regime analysis to cache (separate table)
    try:
        supabase_client.table('market_regime_cache').insert({
            'analysis_data': json.dumps(snapshot),
            'timestamp': now.isoformat()
        }).execute()
        logging.info("Saved regime analysis to cache")
    except Exception as e:
        logging.error(f"Failed to save regime cache: {e}")

    return snapshot


def ai_trade_decision_with_regime(account, strat, sig, sym, size, alert, ai_url):
    """
    Enhanced AI trade decision that includes market regime analysis
    """
    try:
        # Extract base URL properly
        if '/webhook/' in ai_url:
            n8n_base_url = ai_url.split('/webhook/')[0]
        else:
            # Fallback: assume the URL structure
            n8n_base_url = ai_url.replace('/webhook', '')
        
        # Get market regime analysis
        market_analysis = fetch_multi_timeframe_analysis(n8n_base_url)
        
        regime = market_analysis['regime_analysis']
        regime_rules = market_regime_analyzer.get_regime_trading_rules(regime['primary_regime'])
        
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
        
        # Prepare enhanced payload for AI
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
            }
        }
        
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
        
        # Apply position sizing based on regime
        if 'size' in data and regime_rules['max_position_size'] > 0:
            data['size'] = min(data['size'], regime_rules['max_position_size'])
        
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

def get_market_conditions_summary() -> Dict:
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
        
        market_analysis = fetch_multi_timeframe_analysis(n8n_base_url)
        regime = market_analysis['regime_analysis']
        
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
