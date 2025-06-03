#!/usr/bin/env python3
# tradingview_projectx_bot.py

"""
Main entry point for ProjectX Trading Bot.
Handles webhooks, AI decisions, trade execution, and scheduled processing.
"""

from datetime import time as dtime, timedelta
from market_regime import MarketRegime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from logging_config import setup_logging
from config import load_config
from api import (
    flatten_contract, get_contract, ai_trade_decision, cancel_all_stops, 
    search_pos, get_supabase_client, search_trades, search_open,
    get_market_conditions_summary, ai_trade_decision_with_regime
)
from strategies import run_bracket, run_brackmod, run_pivot
from scheduler import start_scheduler
from auth import in_get_flat, authenticate, get_token, get_token_expiry, ensure_token
from signalr_listener import launch_signalr_listener
from threading import Thread
import threading
from datetime import datetime
import logging
import json
import time
from supabase import create_client

# --- Logging/Config/Globals ---
setup_logging()
config = load_config()

TV_PORT         = config['TV_PORT']
WEBHOOK_SECRET  = config['WEBHOOK_SECRET']
ACCOUNTS        = config['ACCOUNTS']
DEFAULT_ACCOUNT = config['DEFAULT_ACCOUNT']
CT              = config['CT']
GET_FLAT_START  = config['GET_FLAT_START']
GET_FLAT_END    = config['GET_FLAT_END']

AI_ENDPOINTS = {
    "epsilon": config['N8N_AI_URL'],
    "beta": config['N8N_AI_URL'],
}

MARKET_SESSIONS = {
    'ASIAN': {
        'start': dtime(17, 0),    # 5 PM CT
        'end': dtime(3, 0),       # 3 AM CT
        'characteristics': 'Lower volatility, trend continuation'
    },
    'LONDON': {
        'start': dtime(2, 0),     # 2 AM CT
        'end': dtime(11, 0),      # 11 AM CT
        'characteristics': 'High volatility, trend initiation'
    },
    'NY_MORNING': {
        'start': dtime(8, 30),    # 8:30 AM CT (Market open)
        'end': dtime(11, 0),      # 11 AM CT
        'characteristics': 'Highest volatility, breakouts'
    },
    'NY_LUNCH': {
        'start': dtime(11, 0),    # 11 AM CT
        'end': dtime(13, 0),      # 1 PM CT
        'characteristics': 'Low volatility, choppy'
    },
    'NY_AFTERNOON': {
        'start': dtime(13, 0),    # 1 PM CT
        'end': dtime(15, 0),      # 3 PM CT (Market close)
        'characteristics': 'Moderate volatility, trend resumption'
    },
    'NY_CLOSE': {
        'start': dtime(14, 30),   # 2:30 PM CT
        'end': dtime(15, 15),     # 3:15 PM CT
        'characteristics': 'Position squaring, volatility spike'
    }
}

def get_current_session(now=None):
    """Get current market session"""
    if now is None:
        now = datetime.now(CT)
    
    current_time = now.time()
    
    for session_name, session_info in MARKET_SESSIONS.items():
        start = session_info['start']
        end = session_info['end']
        
        # Handle sessions that cross midnight
        if start > end:
            if current_time >= start or current_time < end:
                return session_name, session_info
        else:
            if start <= current_time < end:
                return session_name, session_info
    
    return 'OFF_HOURS', {'characteristics': 'Market closed'}


AUTH_LOCK = threading.Lock()

app = Flask(__name__)
CORS(app)  # Enable CORS for the dashboard
# --- Health Check Route (optional, but recommended for uptime monitoring) ---
@app.route("/healthz")
def healthz():
    return jsonify(status="ok", time=str(datetime.now(CT)))

@app.route("/webhook", methods=["POST"])
def tv_webhook():
    data = request.get_json()
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="unauthorized"), 403

    # Respond immediately to TradingView/n8n
    Thread(target=handle_webhook_logic, args=(data,)).start()
    return jsonify(status="accepted", msg="Processing started"), 202

@app.route("/")
def serve_dashboard():
    return send_from_directory('static', 'dashboard.html')

@app.route("/ai/latest-decision", methods=["GET"])
def get_latest_ai_decision():
    """Get the latest AI trading decision from Supabase"""
    try:
        supabase = get_supabase_client()
        
        # Order by ai_decision_id descending to get the latest
        result = supabase.table('ai_trading_log') \
            .select('*') \
            .order('ai_decision_id', desc=True) \
            .limit(1) \
            .execute()
        
        if not result.data or len(result.data) == 0:
            # Fallback to timestamp ordering
            result = supabase.table('ai_trading_log') \
                .select('*') \
                .order('timestamp', desc=True) \
                .limit(1) \
                .execute()
        
        if result.data and len(result.data) > 0:
            latest_decision = result.data[0]  # Get the FIRST item (latest)
            
            # Log for debugging
            logging.info(f"Found AI decision with ai_decision_id: {latest_decision.get('ai_decision_id')}")
            
            # Parse JSON string fields
            json_string_fields = ['urls', 'support', 'resistance', 'trend']
            
            for field in json_string_fields:
                if field in latest_decision and isinstance(latest_decision[field], str) and latest_decision[field]:
                    try:
                        # Only try to parse if it looks like JSON
                        if latest_decision[field].strip().startswith('{') or latest_decision[field].strip().startswith('['):
                            latest_decision[field] = json.loads(latest_decision[field])
                    except json.JSONDecodeError:
                        logging.warning(f"Failed to parse {field} as JSON: {latest_decision[field]}")
            
            # Ensure numeric fields are properly typed
            numeric_fields = ['size', 'tp1', 'tp2', 'tp3', 'sl', 'entrylimit']
            for field in numeric_fields:
                if field in latest_decision and latest_decision[field] is not None:
                    try:
                        if field in ['size', 'tp1', 'tp2', 'tp3', 'sl']:
                            latest_decision[field] = int(latest_decision[field])
                        else:  # entrylimit
                            latest_decision[field] = float(latest_decision[field])
                    except (ValueError, TypeError):
                        pass
            
            # Add a formatted timestamp for display
            if 'timestamp' in latest_decision:
                latest_decision['formatted_timestamp'] = latest_decision['timestamp']
                
            return jsonify(latest_decision), 200
        else:
            return jsonify({
                "error": "No AI decisions found",
                "signal": "HOLD",
                "strategy": "--",
                "size": "--",
                "reason": "No decisions in database"
            }), 404
            
    except Exception as e:
        logging.error(f"Error fetching latest AI decision: {str(e)}")
        logging.error(f"Full error details: {repr(e)}")
        return jsonify({
            "error": str(e),
            "signal": "HOLD",
            "strategy": "--",
            "size": "--"
        }), 500

@app.route("/analysis/latest-all", methods=["GET"])
def get_latest_analysis_all():
    """Get latest analysis for all timeframes from Supabase"""
    try:
        supabase = get_supabase_client()
        
        timeframes = ['5m', '15m', '1h', '1D']
        analysis_data = {}
        
        for tf in timeframes:
            try:
                result = supabase.table('latest_chart_analysis') \
                    .select('*') \
                    .eq('symbol', 'MES') \
                    .eq('timeframe', tf) \
                    .order('timestamp', desc=True) \
                    .limit(1) \
                    .execute()
                
                if result.data:
                    record = result.data[0]
                    snapshot = record.get('snapshot')
                    
                    # Parse JSON if it's a string
                    if isinstance(snapshot, str):
                        try:
                            snapshot = json.loads(snapshot)
                        except:
                            snapshot = {}
                    
                    # Ensure we have the data we need
                    if snapshot:
                        analysis_data[tf] = snapshot
                    else:
                        logging.warning(f"No snapshot data for timeframe {tf}")
                        
            except Exception as e:
                logging.error(f"Error fetching {tf} data: {e}")
        
        return jsonify(analysis_data), 200
        
    except Exception as e:
        logging.error(f"Error fetching analysis data: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/positions/summary", methods=["GET"])
def get_positions_summary():
    """Get a summary of all positions with proper account data"""
    try:
        from position_manager import PositionManager
        from api import get_contract, get_current_market_price
        
        pm = PositionManager(ACCOUNTS)
        cid = get_contract('MES')
        
        # Get current market price
        try:
            current_price, price_source = get_current_market_price(symbol="MES")
        except:
            current_price = None
            price_source = "unavailable"
        
        summary = {
            'market_price': current_price,
            'price_source': price_source,
            'accounts': {},
            'total_positions': 0,
            'total_unrealized_pnl': 0,
            'total_realized_pnl': 0,
            'total_daily_pnl': 0,
            'timestamp': datetime.now(CT).isoformat()
        }
        
        for account_name, acct_id in ACCOUNTS.items():
            try:
                position_state = pm.get_position_state(acct_id, cid)
                account_state = pm.get_account_state(acct_id)
                
                account_data = {
                    'position': None,
                    'daily_stats': {
                        'daily_pnl': account_state['daily_pnl'],
                        'trades_today': account_state['trade_count'],
                        'win_rate': f"{account_state['win_rate']:.1%}" if account_state['win_rate'] else "0.0%",
                        'winning_trades': account_state['winning_trades'],
                        'losing_trades': account_state['losing_trades'],
                        'consecutive_losses': account_state['consecutive_losses'],
                        'can_trade': account_state['can_trade'],
                        'risk_level': account_state['risk_level']
                    }
                }
                
                # Add to total daily P&L
                summary['total_daily_pnl'] += account_state['daily_pnl']
                
                if position_state['has_position']:
                    # Calculate current P&L if we have market price
                    if current_price and position_state['entry_price']:
                        contract_multiplier = 5  # MES multiplier
                        if position_state['side'] == 'LONG':
                            unrealized_pnl = (current_price - position_state['entry_price']) * position_state['size'] * contract_multiplier
                        elif position_state['side'] == 'SHORT':
                            unrealized_pnl = (position_state['entry_price'] - current_price) * position_state['size'] * contract_multiplier
                        else:
                            unrealized_pnl = 0
                    else:
                        unrealized_pnl = position_state.get('unrealized_pnl', 0)
                    
                    account_data['position'] = {
                        'size': position_state['size'],
                        'side': position_state['side'],
                        'entry_price': position_state['entry_price'],
                        'current_price': current_price or position_state.get('current_price'),
                        'unrealized_pnl': unrealized_pnl,
                        'realized_pnl': position_state.get('realized_pnl', 0),
                        'total_pnl': position_state.get('realized_pnl', 0) + unrealized_pnl,
                        'duration_minutes': int(position_state['duration_minutes']),
                        'stops': len(position_state['stop_orders']),
                        'targets': len(position_state['limit_orders'])
                    }
                    
                    summary['total_positions'] += 1
                    summary['total_unrealized_pnl'] += unrealized_pnl
                    summary['total_realized_pnl'] += position_state.get('realized_pnl', 0)
                
                summary['accounts'][account_name] = account_data
                
            except Exception as e:
                logging.error(f"Error getting data for account {account_name}: {e}")
                summary['accounts'][account_name] = {
                    'error': str(e),
                    'position': None,
                    'daily_stats': {
                        'daily_pnl': 0,
                        'trades_today': 0,
                        'win_rate': '0.0%',
                        'can_trade': False,
                        'risk_level': 'unknown'
                    }
                }
        
        # Add summary stats
        summary['summary'] = {
            'total_pnl': summary['total_unrealized_pnl'] + summary['total_realized_pnl'],
            'positions_open': summary['total_positions'],
            'market_status': 'OPEN' if not in_get_flat(datetime.now(CT)) else 'FLAT_WINDOW'
        }
        
        return jsonify(summary), 200
        
    except Exception as e:
        logging.error(f"Error getting positions summary: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/positions", methods=["GET"])
def get_positions():
    """Get current positions across all accounts"""
    try:
        from api import get_all_positions_summary
        summary = get_all_positions_summary()
        return jsonify(summary), 200
    except Exception as e:
        logging.error(f"Error getting positions: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/positions/<account>", methods=["GET"])
def get_account_positions(account):
    """Get positions for a specific account"""
    try:
        from position_manager import PositionManager
        from api import get_contract
        
        acct_id = ACCOUNTS.get(account.lower())
        if not acct_id:
            return jsonify({"error": f"Unknown account: {account}"}), 404
        
        pm = PositionManager(ACCOUNTS)
        cid = get_contract('MES')
        
        position_state = pm.get_position_state(acct_id, cid)
        account_state = pm.get_account_state(acct_id)
        
        return jsonify({
            "account": account,
            "position": position_state,
            "account_metrics": account_state,
            "timestamp": datetime.now(CT).isoformat()
        }), 200
        
    except Exception as e:
        logging.error(f"Error getting account positions: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/positions/<account>/manage", methods=["POST"])
def get_position_suggestions(account):
    """Get AI suggestions for position management (no autonomous actions)"""
    try:
        data = request.get_json()
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify(error="unauthorized"), 403
        
        from position_manager import PositionManager
        from api import get_contract
        
        acct_id = ACCOUNTS.get(account.lower())
        if not acct_id:
            return jsonify({"error": f"Unknown account: {account}"}), 404
        
        pm = PositionManager(ACCOUNTS)
        cid = get_contract('MES')
        
        # Get full context for AI
        context = pm.get_position_context_for_ai(acct_id, cid)
        
        # Instead of managing positions, return context and suggestions
        return jsonify({
            "account": account,
            "context": context,
            "message": "Position context provided - AI should make all trading decisions",
            "timestamp": datetime.now(CT).isoformat()
        }), 200
        
    except Exception as e:
        logging.error(f"Error getting position suggestions: {e}")
        return jsonify({"error": str(e)}), 500

'''
@app.route("/scan", methods=["POST"])
def scan_opportunities():
    """Manually trigger opportunity scan"""
    try:
        data = request.get_json()
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify(error="unauthorized"), 403
        
        from position_manager import PositionManager
        
        pm = PositionManager(ACCOUNTS)
        opportunities = []
        
        for account_name, acct_id in ACCOUNTS.items():
            opportunity = pm.scan_for_opportunities(acct_id, account_name)
            if opportunity:
                opportunities.append(opportunity)
        
        return jsonify({
            "opportunities": opportunities,
            "count": len(opportunities),
            "timestamp": datetime.now(CT).isoformat()
        }), 200
        
    except Exception as e:
        logging.error(f"Error scanning opportunities: {e}")
        return jsonify({"error": str(e)}), 500
'''
@app.route("/account/<account>/health", methods=["GET"])
def get_account_health(account):
    """Get account health metrics"""
    try:
        from position_manager import PositionManager
        
        acct_id = ACCOUNTS.get(account.lower())
        if not acct_id:
            return jsonify({"error": f"Unknown account: {account}"}), 404
        
        pm = PositionManager(ACCOUNTS)
        account_state = pm.get_account_state(acct_id)
        
        # Add risk thresholds for context
        account_state['thresholds'] = {
            'max_daily_loss': pm.max_daily_loss,
            'profit_target': pm.profit_target,
            'max_consecutive_losses': pm.max_consecutive_losses
        }
        
        return jsonify({
            "account": account,
            "health": account_state,
            "timestamp": datetime.now(CT).isoformat()
        }), 200
        
    except Exception as e:
        logging.error(f"Error getting account health: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/market", methods=["GET"])
def get_market_status():
    """Get current market conditions and regime"""
    try:
        summary = get_market_conditions_summary()
        
        # Add session info
        session_name, session_info = get_current_session()
        summary['session'] = {
            'name': session_name,
            'characteristics': session_info.get('characteristics', 'Unknown')
        }
        
        return jsonify(summary), 200
        
    except Exception as e:
        logging.error(f"Error getting market status: {e}")
        return jsonify({"error": str(e)}), 500
'''
@app.route("/autonomous/toggle", methods=["POST"])
def toggle_autonomous():
    """Toggle autonomous trading on/off"""
    try:
        data = request.get_json()
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify(error="unauthorized"), 403
        
        enabled = data.get("enabled", True)
        
        # This would need to be implemented with a global flag
        # For now, just return status
        return jsonify({
            "autonomous_trading": enabled,
            "message": "Autonomous trading " + ("enabled" if enabled else "disabled"),
            "timestamp": datetime.now(CT).isoformat()
        }), 200
        
    except Exception as e:
        logging.error(f"Error toggling autonomous: {e}")
        return jsonify({"error": str(e)}), 500
'''
@app.route("/positions/realtime", methods=["GET"])
def get_realtime_positions():
    """Get real-time positions with current P&L across all accounts"""
    try:
        from position_manager import PositionManager
        from api import get_contract, get_current_market_price
        
        pm = PositionManager(ACCOUNTS)
        cid = get_contract('MES')
        
        # Get current market price
        current_price, price_source = get_current_market_price(symbol="MES")
        
        results = {
            'market_price': current_price,
            'price_source': price_source,
            'accounts': {},
            'total_unrealized_pnl': 0,
            'total_realized_pnl': 0,
            'timestamp': datetime.now(CT).isoformat()
        }
        
        for account_name, acct_id in ACCOUNTS.items():
            position_state = pm.get_position_state(acct_id, cid)
            
            if position_state['has_position']:
                account_info = {
                    'position': {
                        'size': position_state['size'],
                        'side': position_state['side'],
                        'entry_price': position_state['entry_price'],
                        'current_price': position_state.get('current_price'),
                        'unrealized_pnl': position_state.get('unrealized_pnl', 0),
                        'realized_pnl': position_state.get('realized_pnl', 0),
                        'total_pnl': position_state['current_pnl'],
                        'duration_minutes': position_state['duration_minutes'],
                        'stops': len(position_state['stop_orders']),
                        'targets': len(position_state['limit_orders'])
                    }
                }
                
                # Add to totals
                results['total_unrealized_pnl'] += position_state.get('unrealized_pnl', 0)
                results['total_realized_pnl'] += position_state.get('realized_pnl', 0)
            else:
                account_info = {'position': None}
            
            # Add account state
            account_state = pm.get_account_state(acct_id)
            account_info['daily_stats'] = {
                'daily_pnl': account_state['daily_pnl'],
                'trades_today': account_state['trade_count'],
                'win_rate': f"{account_state['win_rate']:.1%}",
                'can_trade': account_state['can_trade'],
                'risk_level': account_state['risk_level']
            }
            
            results['accounts'][account_name] = account_info
        
        # Add summary
        results['summary'] = {
            'total_pnl': results['total_unrealized_pnl'] + results['total_realized_pnl'],
            'positions_open': sum(1 for acc in results['accounts'].values() if acc['position']),
            'market_status': 'OPEN' if not in_get_flat(datetime.now(CT)) else 'FLAT_WINDOW'
        }
        
        return jsonify(results), 200
        
    except Exception as e:
        logging.error(f"Error getting real-time positions: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/debug/positions", methods=["GET"])
def debug_positions():
    """Debug position data and P&L calculations"""
    try:
        from api import search_pos, get_current_market_price, get_contract
        
        cid = get_contract('MES')
        price, price_source = get_current_market_price()
        
        result = {
            'current_price': price,
            'price_source': price_source,
            'contract_id': cid,
            'accounts': {}
        }
        
        for account_name, acct_id in ACCOUNTS.items():
            positions = search_pos(acct_id)
            
            # Filter for our contract
            contract_positions = [p for p in positions if p.get("contractId") == cid]
            
            account_info = {
                'all_positions': positions,
                'contract_positions': contract_positions,
                'calculations': []
            }
            
            # Calculate P&L for each position
            for pos in contract_positions:
                size = pos.get('size', 0)
                entry = pos.get('averagePrice', 0)
                pos_type = pos.get('type')
                
                calc = {
                    'position_id': pos.get('id'),
                    'type': pos_type,
                    'type_meaning': 'LONG' if pos_type == 1 else 'SHORT' if pos_type == 2 else 'UNKNOWN',
                    'size': size,
                    'entry_price': entry,
                    'current_price': price
                }
                
                if price and entry and size > 0:
                    if pos_type == 1:  # Long
                        move = price - entry
                        pnl = move * size * 5
                        calc['calculation'] = f"({price} - {entry}) * {size} * 5 = {pnl}"
                    elif pos_type == 2:  # Short
                        move = entry - price
                        pnl = move * size * 5
                        calc['calculation'] = f"({entry} - {price}) * {size} * 5 = {pnl}"
                    else:
                        pnl = 0
                        calc['calculation'] = f"Unknown position type: {pos_type}"
                    
                    calc['price_move'] = move
                    calc['unrealized_pnl'] = pnl
                
                account_info['calculations'].append(calc)
            
            result['accounts'][account_name] = account_info
        
        return jsonify(result), 200
        
    except Exception as e:
        import traceback
        return jsonify({
            'error': str(e),
            'traceback': traceback.format_exc()
        }), 500

@app.route("/position-context/<account>", methods=["GET"])
def get_position_context(account):
    """Get position context for AI decision making"""
    try:
        from position_manager import PositionManager
        from api import get_contract
        
        acct_id = ACCOUNTS.get(account.lower())
        if not acct_id:
            return jsonify({"error": f"Unknown account: {account}"}), 404
        
        pm = PositionManager(ACCOUNTS)
        cid = get_contract('MES')
        
        context = pm.get_position_context_for_ai(acct_id, cid)
        
        return jsonify({
            "account": account,
            "position_context": context,
            "timestamp": datetime.now(CT).isoformat()
        }), 200
        
    except Exception as e:
        logging.error(f"Error getting position context: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/debug/price-sources", methods=["GET"])
def debug_price_sources():
    """Debug what price data is available"""
    try:
        supabase = get_supabase_client()
        
        # Check tv_datafeed
        tv_result = supabase.table('tv_datafeed').select('*').limit(5).execute()
        
        # Check latest_chart_analysis
        chart_result = supabase.table('latest_chart_analysis').select('*').limit(5).execute()
        
        return jsonify({
            'tv_datafeed_count': len(tv_result.data) if tv_result.data else 0,
            'tv_datafeed_sample': tv_result.data[0] if tv_result.data else None,
            'chart_analysis_count': len(chart_result.data) if chart_result.data else 0,
            'chart_analysis_sample': chart_result.data[0] if chart_result.data else None
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route("/contracts/<symbol>", methods=["GET"])
def get_symbol_contracts(symbol):
    """Get all contracts for a symbol"""
    try:
        contracts = search_contracts(symbol.upper())
        active = [c for c in contracts if c.get("activeContract")]
        
        return jsonify({
            "symbol": symbol,
            "total_contracts": len(contracts),
            "active_contracts": len(active),
            "contracts": contracts,
            "selected": get_active_contract_for_symbol(symbol.upper()) if active else None
        }), 200
        
    except Exception as e:
        logging.error(f"Error getting contracts: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/contracts/current", methods=["GET"])
def get_current_contracts():
    """Get current active contracts for configured symbols"""
    try:
        symbols = ["MES", "ES", "NQ", "MNQ"]  # Add more as needed
        result = {}
        
        for symbol in symbols:
            contract_id = get_active_contract_for_symbol_cached(symbol)
            if contract_id:
                result[symbol] = contract_id
        
        return jsonify({
            "contracts": result,
            "cache_duration_seconds": CONTRACT_CACHE_DURATION,
            "timestamp": datetime.now(CT).isoformat()
        }), 200
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def handle_webhook_logic(data):
    try:
        strat = data.get("strategy", "bracket").lower()
        acct  = (data.get("account") or DEFAULT_ACCOUNT).lower()
        sig   = data.get("signal", "").upper()
        sym   = data.get("symbol", config['DEFAULT_SYMBOL'])  # Use default symbol if not provided
        size  = int(data.get("size", 1))
        alert = data.get("alert", "")
        ai_decision_id = data.get("ai_decision_id", None)
        regime = "unknown"
        regime_confidence = 0

        if acct not in ACCOUNTS:
            logging.error(f"Unknown account '{acct}'")
            return

        acct_id = ACCOUNTS[acct]
        cid = get_contract(sym)
        if not cid:
            logging.error(f"Could not determine contract ID for symbol {sym}")
            return

        # Manual flatten (close all) signal
        if sig == "FLAT":
            flatten_contract(acct_id, cid, timeout=10)
            logging.info(f"Manual flatten signal processed for {acct_id} {cid}")
            return

        now = datetime.now(CT)
        
        # Check if in get-flat window
        if in_get_flat(now):
            logging.info("In get-flat window, no trades processed")
            return
        
        # Get current market session
        session_name, session_info = get_current_session(now)
        logging.info(f"Current session: {session_name} - {session_info['characteristics']}")
        
        # Log market conditions periodically
        if not hasattr(handle_webhook_logic, 'last_market_log'):
            handle_webhook_logic.last_market_log = now - timedelta(minutes=6)
        
        if now - handle_webhook_logic.last_market_log > timedelta(minutes=5):
            get_market_conditions_summary()
            handle_webhook_logic.last_market_log = now

        # --- AI Overseer OR Direct Trading ---
        if acct in AI_ENDPOINTS:
            # AI path - existing code
            ai_url = AI_ENDPOINTS[acct]
            ai_decision = ai_trade_decision_with_regime(
                acct, strat, sig, sym, size, alert, ai_url
            )
            
            regime = ai_decision.get('regime', 'unknown')
            regime_confidence = ai_decision.get('regime_confidence', 0)
            logging.info(f"Market regime: {regime} (confidence: {regime_confidence}%)")
            
            if ai_decision.get("signal", "").upper() not in ("BUY", "SELL"):
                logging.info(f"AI blocked trade: {ai_decision.get('reason', 'No reason')}")
                return
                
            # Overwrite with AI decision
            strat = ai_decision.get("strategy", strat)
            sig = ai_decision.get("signal", sig)
            sym = ai_decision.get("symbol", sym)
            size = ai_decision.get("size", size)
            alert = ai_decision.get("alert", alert)
            ai_decision_id = ai_decision.get("ai_decision_id", ai_decision_id)
        else:
            # NON-AI ACCOUNT - Create a simple decision ID for tracking
            ai_decision_id = int(time.time() * 1000) % (2**62)
            logging.info(f"Non-AI account {acct} - proceeding with manual trade")

        # Check current positions before entry
        positions = search_pos(acct_id)
        open_pos = [p for p in positions if p["contractId"] == cid and p.get("size", 0) != 0]
        
        if not open_pos:
            cancel_all_stops(acct_id, cid)
        else:
            total_size = sum(p.get("size", 0) for p in open_pos)
            logging.info(f"Current position size: {total_size}")

        # --- Strategy Dispatch ---
        logging.info(f"Executing {strat} strategy: {sig} {size} {sym} in {regime} regime")
        
        if strat == "bracket":
            run_bracket(acct_id, sym, sig, size, alert, ai_decision_id)
        elif strat == "brackmod":
            run_brackmod(acct_id, sym, sig, size, alert, ai_decision_id)
        elif strat == "pivot":
            run_pivot(acct_id, sym, sig, size, alert, ai_decision_id)
        else:
            logging.error(f"Unknown strategy '{strat}'")
            
    except Exception as e:
        import traceback
        logging.error(f"Exception in handle_webhook_logic: {e}\n{traceback.format_exc()}")

def scheduled_market_analysis():
    """Run market analysis every 15 minutes"""
    try:
        summary = get_market_conditions_summary()
        
        # You could also send this to Supabase or Discord
        if summary['regime'] == 'choppy' and summary['confidence'] > 80:
            logging.warning("High confidence choppy market detected - be cautious!")
            
    except Exception as e:
        logging.error(f"Error in scheduled market analysis: {e}")

if __name__ == "__main__":
    try:
        authenticate()
        signalr_listener = launch_signalr_listener(
            get_token=get_token,
            get_token_expiry=get_token_expiry,
            authenticate=authenticate,
            auth_lock=AUTH_LOCK
        ) 
        scheduler = start_scheduler(app)
        app.logger.info("Starting server.")
        app.run(host="0.0.0.0", port=TV_PORT, threaded=True)
    except Exception as e:
        logging.exception(f"Fatal error during startup: {e}")
