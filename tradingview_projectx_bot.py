#!/usr/bin/env python3
# tradingview_projectx_bot.py

"""
Main entry point for ProjectX Trading Bot.
Handles webhooks, AI decisions, trade execution, and scheduled processing.
"""

from datetime import time as dtime, timedelta
from typing import Optional
from market_regime import MarketRegime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from logging_config import setup_logging
from config import load_config
from api import (
    flatten_contract, get_contract,
    search_pos, get_supabase_client,
    get_market_conditions_summary, ai_trade_decision_with_regime,
    # Added for /contracts endpoints
    search_contracts, get_active_contract_for_symbol_cached,
    get_active_contract_for_symbol,  # used in JSON response
    CONTRACT_CACHE_DURATION,
    # NEW: balances via ProjectX
    search_accounts,  # <—— uses /api/Account/search
)
from strategies import run_simple_bracket
from scheduler import start_scheduler
from auth import in_get_flat, authenticate, get_token, get_token_expiry, ensure_token
from signalr_listener import launch_signalr_listener
from threading import Thread
import threading
from datetime import datetime
import logging
import json
import time
import os
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

AI_ENDPOINTS = config['AI_ENDPOINTS']

def ai_url_for(account_name: str) -> str:
    url = AI_ENDPOINTS.get(account_name)
    if not url:
        raise RuntimeError(
            f"No AI endpoint for account '{account_name}'. Known: {list(AI_ENDPOINTS)}"
        )
    return url

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

# --- STATIC DASHBOARD + DATA ENDPOINTS (UNCOMMENTED) ---

@app.route("/")
def serve_dashboard():
    # serve ./static/dashboard.html
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    return send_from_directory(static_dir, "dashboard.html")

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
            latest_decision = result.data[0]
            logging.info(f"Found AI decision with ai_decision_id: {latest_decision.get('ai_decision_id')}")
            # Parse JSON-like string fields if needed
            json_string_fields = ['urls', 'support', 'resistance', 'trend']
            for field in json_string_fields:
                if field in latest_decision and isinstance(latest_decision[field], str) and latest_decision[field]:
                    try:
                        txt = latest_decision[field].strip()
                        if txt.startswith('{') or txt.startswith('['):
                            latest_decision[field] = json.loads(txt)
                    except json.JSONDecodeError:
                        logging.warning(f"Failed to parse {field} as JSON: {latest_decision[field]}")
            # Coerce numeric fields
            for field in ['size', 'tp1', 'tp2', 'tp3', 'sl', 'entrylimit']:
                if field in latest_decision and latest_decision[field] is not None:
                    try:
                        latest_decision[field] = float(latest_decision[field])
                    except (ValueError, TypeError):
                        pass
            # Add display timestamp
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
        timeframes = ['5m', '15m', '30m']  # align with your n8n workflows
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
                    if isinstance(snapshot, str):
                        try:
                            snapshot = json.loads(snapshot)
                        except:
                            snapshot = {}
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
    """Get a summary of all positions with proper account data + broker balances"""
    try:
        from position_manager import PositionManager
        from api import get_contract, get_current_market_price

        pm = PositionManager(ACCOUNTS)
        cid = get_contract('MES')

        # Current market price
        try:
            current_price, price_source = get_current_market_price(symbol="MES")
        except Exception:
            current_price = None
            price_source = "unavailable"

        # --- NEW: one call to get all accounts + balances from ProjectX
        balances_by_id = {}
        try:
            # { account_id: {"name":..., "balance": float, "canTrade": bool, "isVisible": bool}, ... }
            balances_by_id = search_accounts(only_active=True)
        except Exception as e:
            logging.warning(f"Account search failed (balances unavailable): {e}")

        def _is_practice_account(name: str, balance_info: Optional[dict] = None) -> bool:
            """Determine if an account should be hidden from dashboards (practice/demo)."""
            tokens = (name or '').lower()
            practice_markers = ('practice', 'sim', 'simulation', 'paper', 'demo')
            if any(marker in tokens for marker in practice_markers):
                return True

            if balance_info:
                for candidate_key in ('name', 'accountType', 'account_type', 'label'):
                    candidate_val = balance_info.get(candidate_key)
                    if isinstance(candidate_val, str) and any(
                        marker in candidate_val.lower() for marker in practice_markers
                    ):
                        return True

            return False

        summary = {
            'market_price': current_price,
            'price_source': price_source,
            'accounts': {},
            'total_positions': 0,
            'total_unrealized_pnl': 0,
            'total_realized_pnl': 0,
            'total_daily_pnl': 0,
            'total_fees': 0.0,
            'total_equity': 0.0,          # <—— NEW rollup
            'timestamp': datetime.now(CT).isoformat()
        }

        for account_name, acct_id in ACCOUNTS.items():
            bal_info = balances_by_id.get(acct_id)

            if _is_practice_account(account_name, bal_info):
                logging.debug(f"Skipping practice account {account_name}")
                continue

            try:
                position_state = pm.get_position_state(acct_id, cid)
                account_state = pm.get_account_state(acct_id)

                account_data = {
                    'position': None,
                    'daily_stats': {
                        'daily_pnl': account_state['daily_pnl'],
                        'daily_fees': account_state.get('daily_fees', 0.0),
                        'gross_pnl': account_state.get('gross_pnl', account_state['daily_pnl']),
                        'trades_today': account_state['trade_count'],
                        'win_rate': f"{account_state['win_rate']:.1%}" if account_state['win_rate'] else "0.0%",
                        'winning_trades': account_state['winning_trades'],
                        'losing_trades': account_state['losing_trades'],
                        'consecutive_losses': account_state['consecutive_losses'],
                        'can_trade': account_state['can_trade'],
                        'risk_level': account_state['risk_level']
                    }
                }

                # Attach live broker balance if available
                if bal_info:
                    try:
                        bal_val = float(bal_info.get("balance"))
                    except (TypeError, ValueError):
                        bal_val = None
                    account_data['balances'] = {
                        'balance': bal_val,
                        'canTrade': bal_info.get('canTrade'),
                        'isVisible': bal_info.get('isVisible'),
                        'name': bal_info.get('name'),
                    }
                    if isinstance(bal_val, float):
                        summary['total_equity'] += bal_val

                summary['total_daily_pnl'] += account_state['daily_pnl']
                summary['total_fees'] += account_state.get('daily_fees', 0.0)

                if position_state['has_position']:
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

        summary['summary'] = {
            'total_pnl': summary['total_unrealized_pnl'] + summary['total_realized_pnl'],
            'positions_open': summary['total_positions'],
            'market_status': 'OPEN' if not in_get_flat(datetime.now(CT)) else 'FLAT_WINDOW'
        }

        # If no balances were fetched, avoid returning 0.0 which can be misleading
        if not balances_by_id:
            summary['total_equity'] = None

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

        context = pm.get_position_context_for_ai(acct_id, cid)

        return jsonify({
            "account": account,
            "context": context,
            "message": "Position context provided - AI should make all trading decisions",
            "timestamp": datetime.now(CT).isoformat()
        }), 200

    except Exception as e:
        logging.error(f"Error getting position suggestions: {e}")
        return jsonify({"error": str(e)}), 500

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
            # Placeholder: implement scan inside PositionManager if desired
            # opportunity = pm.scan_for_opportunities(acct_id, account_name)
            opportunity = None
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
    """Get current market conditions and regime (resilient to None)."""
    try:
        summary = get_market_conditions_summary() or {}
        session_name, session_info = get_current_session()
        summary['session'] = {
            'name': session_name,
            'characteristics': session_info.get('characteristics', 'Unknown')
        }
        # sane defaults if upstream omitted them
        summary.setdefault('regime', 'unknown')
        summary.setdefault('confidence', 0)
        return jsonify(summary), 200
    except Exception as e:
        logging.error(f"Error getting market status: {e}")
        return jsonify({"error": str(e), "regime": "unknown", "confidence": 0}), 200


@app.route("/autonomous/toggle", methods=["POST"])
def toggle_autonomous():
    """Toggle autonomous trading on/off"""
    try:
        data = request.get_json()
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify(error="unauthorized"), 403
        enabled = data.get("enabled", True)
        return jsonify({
            "autonomous_trading": enabled,
            "message": "Autonomous trading " + ("enabled" if enabled else "disabled"),
            "timestamp": datetime.now(CT).isoformat()
        }), 200
    except Exception as e:
        logging.error(f"Error toggling autonomous: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/positions/realtime", methods=["GET"])
def get_realtime_positions():
    """Get real-time positions with current P&L across all accounts"""
    try:
        from position_manager import PositionManager
        from api import get_contract, get_current_market_price

        pm = PositionManager(ACCOUNTS)
        cid = get_contract('MES')

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
                results['total_unrealized_pnl'] += position_state.get('unrealized_pnl', 0)
                results['total_realized_pnl'] += position_state.get('realized_pnl', 0)
            else:
                account_info = {'position': None}

            account_state = pm.get_account_state(acct_id)
            account_info['daily_stats'] = {
                'daily_pnl': account_state['daily_pnl'],
                'trades_today': account_state['trade_count'],
                'win_rate': f"{account_state['win_rate']:.1%}",
                'can_trade': account_state['can_trade'],
                'risk_level': account_state['risk_level']
            }

            results['accounts'][account_name] = account_info

        results['summary'] = {
            'total_pnl': results['total_unrealized_pnl'] + results['total_realized_pnl'],
            'positions_open': sum(1 for acc in results['accounts'].values() if acc['position']),
            'market_status': 'OPEN' if not in_get_flat(datetime.now(CT)) else 'FLAT_WINDOW'
        }

        return jsonify(results), 200

    except Exception as e:
        logging.error(f"Error getting real-time positions: {e}")
        return jsonify({"error": str(e)}), 500

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

# --- CONTRACTS ENDPOINTS ---

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
        symbols = ["MES", "ES", "NQ", "MNQ"]
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


def flatten_account_positions(acct_id: int, primary_cid: Optional[str], symbol: Optional[str]) -> None:
    """Flatten all open positions for an account, prioritizing the provided contract."""
    flattened_contracts = []

    if primary_cid:
        if flatten_contract(acct_id, primary_cid, timeout=10):
            flattened_contracts.append(primary_cid)
        else:
            logging.error(
                "Manual flatten for %s failed on primary contract %s", acct_id, primary_cid
            )
    else:
        logging.warning(
            "Manual flatten for %s: symbol '%s' did not resolve to a contract; scanning all open positions",
            acct_id,
            symbol,
        )

    try:
        positions = search_pos(acct_id) or []
    except Exception as exc:
        logging.error("Manual flatten for %s: unable to query positions (%s)", acct_id, exc)
        positions = []

    for pos in positions:
        pos_cid = pos.get("contractId")
        size = pos.get("size", 0)
        if not pos_cid or pos_cid in flattened_contracts or not size:
            continue
        if flatten_contract(acct_id, pos_cid, timeout=10):
            flattened_contracts.append(pos_cid)
        else:
            logging.error("Manual flatten for %s failed on contract %s", acct_id, pos_cid)

    if flattened_contracts:
        logging.info(
            "Manual flatten complete for %s -> flattened %s",
            acct_id,
            ", ".join(flattened_contracts),
        )
    else:
        logging.info("Manual flatten for %s: no open positions found", acct_id)


def handle_webhook_logic(data):
    try:
        strat = data.get("strategy", "simple_bracket").lower()
        acct  = (data.get("account") or DEFAULT_ACCOUNT).lower()
        direction = data.get("signal", "").upper()
        action = (data.get("action") or direction or "").upper()
        sym   = data.get("symbol", config['DEFAULT_SYMBOL'])
        size  = int(data.get("size", 1))
        alert = data.get("alert", "")
        ai_decision_id = data.get("ai_decision_id", None)
        template = data.get("bracket_template") or data.get("template")
        regime = "unknown"
        regime_confidence = 0

        if acct not in ACCOUNTS:
            logging.error(f"Unknown account '{acct}'")
            return

        if not strat:
            logging.info(f"No strategy provided for account {acct}; skipping trade execution")
            return

        if not direction:
            logging.info(f"No signal provided for account {acct}; skipping trade execution")
            return

        acct_id = ACCOUNTS[acct]

        now = datetime.now(CT)

        # Check if in get-flat window
        if in_get_flat(now):
            logging.info("In get-flat window, no trades processed")
            return

        # --- AI Overseer OR Direct Trading ---
        ai_decision = None
        if acct in AI_ENDPOINTS:
            ai_url = AI_ENDPOINTS[acct]
            ai_decision = ai_trade_decision_with_regime(
                acct, strat, direction, sym, size, alert, ai_url
            )

            regime = ai_decision.get('regime', 'unknown')
            regime_confidence = ai_decision.get('regime_confidence', 0)
            logging.info(f"Market regime: {regime} (confidence: {regime_confidence}%)")

            # Merge / normalize
            strat = (ai_decision.get("strategy", strat) or "simple_bracket").lower()
            direction = (ai_decision.get("signal", direction) or "").upper()
            action = (ai_decision.get("action", action) or direction).upper()
            sym   = ai_decision.get("symbol", sym)
            size  = ai_decision.get("size", size)
            alert = ai_decision.get("alert", alert)
            template = ai_decision.get("bracket_template", template)
            ai_decision_id = ai_decision.get("ai_decision_id", ai_decision_id)

            # allow BUY/SELL/HOLD/FLAT to continue; block anything else
            if direction not in ("BUY", "SELL", "HOLD", "FLAT") and action not in ("ENTER", "EXIT", "ADD", "REDUCE", "NO_POSITION"):
                logging.info(
                    f"AI decided {direction or action}; no action. Reason: {ai_decision.get('reason', 'n/a')}"
                )
                return
        else:
            # NON-AI ACCOUNT - simple decision ID for tracking
            ai_decision_id = int(time.time() * 1000) % (2**62)
            logging.info(f"Non-AI account {acct} - proceeding with manual trade")

        # --- FLAT handling for manual or AI decisions ---
        if action in ("FLAT", "EXIT", "REDUCE") or direction == "FLAT":
            try:
                primary_cid = get_contract(sym)  # may be None; sweeper tolerates it
            except Exception:
                primary_cid = None
            flatten_account_positions(acct_id, primary_cid, sym)
            if ai_decision:
                logging.info(
                    f"AI-initiated FLAT complete for account {acct} ({sym}) decision_id={ai_decision_id}"
                )
            else:
                logging.info(
                    f"Manual FLAT complete for account {acct} ({sym})"
                )
            return

        if action in ("HOLD", "NO_POSITION") or direction == "HOLD":
            logging.info("HOLD signal received; no bracket submitted")
            return

        try:
            cid = get_contract(sym)
        except Exception:
            cid = None
        if not cid:
            logging.error("Could not determine contract ID for symbol %s; aborting entry", sym)
            return

        logging.info(
            f"Executing {strat} strategy: {direction or action} {size} {sym} in {regime} regime"
        )

        if direction in ("BUY", "SELL"):
            run_simple_bracket(
                acct_id=acct_id,
                account_name=acct,
                sym=sym,
                direction=direction,
                size=size,
                alert=alert,
                ai_decision_id=ai_decision_id,
                template=template,
                regime=regime,
            )
        else:
            logging.info("No actionable BUY/SELL direction provided; skipping order submission")

    except Exception as e:
        import traceback
        logging.error(f"Exception in handle_webhook_logic: {e}\n{traceback.format_exc()}")

'''
def scheduled_market_analysis():
    """Run market analysis every 15 minutes"""
    try:
        summary = get_market_conditions_summary()
        if summary['regime'] == 'choppy' and summary['confidence'] > 80:
            logging.warning("High confidence choppy market detected - be cautious!")
    except Exception as e:
        logging.error(f"Error in scheduled market analysis: {e}")
'''

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
