# api.py
import requests
import logging
import json
import time
import pytz
import threading
import hashlib
import random  # NEW: for jitter in backoff

from datetime import datetime, timezone, timedelta
from auth import ensure_token, get_token
from config import load_config
from dateutil import parser
from market_regime import MarketRegime
from market_regime_ohlc import OHLCRegimeDetector
from supabase import create_client, Client
from typing import Dict, List, Optional, Tuple
from threading import RLock

# ─────────────────────────────
# Tiny in-memory TTL cache (already present)
# ─────────────────────────────
_CACHE = {}
_CACHE_LOCK = RLock()

def _cache_get(key: str):
    with _CACHE_LOCK:
        rec = _CACHE.get(key)
        if not rec:
            return None
        val, exp = rec
        if exp < time.time():
            _CACHE.pop(key, None)
            return None
        return val

def _cache_set(key: str, val, ttl: float):
    with _CACHE_LOCK:
        _CACHE[key] = (val, time.time() + ttl)

session = requests.Session()
config = load_config()
ACCOUNTS = config['ACCOUNTS'] 
OVERRIDE_CONTRACT_ID = config['OVERRIDE_CONTRACT_ID']
PX_BASE = config['PX_BASE']
SUPABASE_URL = config['SUPABASE_URL']
SUPABASE_KEY = config['SUPABASE_KEY']
CT = pytz.timezone("America/Chicago")

# Add to the top of api.py after imports
contract_cache = {}
contract_cache_expiry = {}
CONTRACT_CACHE_DURATION = 3600  # Cache for 1 hour

def get_active_contract_for_symbol_cached(symbol: str, live: bool = False) -> Optional[str]:
    """
    Get active contract with caching to avoid repeated API calls
    """
    cache_key = f"{symbol}_{live}"
    current_time = time.time()
    
    # Check cache
    if cache_key in contract_cache and cache_key in contract_cache_expiry:
        if current_time < contract_cache_expiry[cache_key]:
            logging.debug(f"Using cached contract for {symbol}: {contract_cache[cache_key]}")
            return contract_cache[cache_key]
    
    # Fetch fresh data
    contract_id = get_active_contract_for_symbol(symbol, live)
    
    # Update cache
    if contract_id:
        contract_cache[cache_key] = contract_id
        contract_cache_expiry[cache_key] = current_time + CONTRACT_CACHE_DURATION
    
    return contract_id

# Global lock for regime cache updates
regime_cache_lock = threading.Lock()
regime_cache_in_progress = {}

# ─────────────────────────────
# Low-level POST helper
# ─────────────────────────────
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

# ─────────────────────────────
# Robust POST with retries/backoff (used by hot endpoints)
# ─────────────────────────────
DEFAULT_MAX_RETRIES = 3
BASE_DELAY = 0.5  # seconds

def _post_with_retry(path: str, payload: dict, max_retries: int = DEFAULT_MAX_RETRIES):
    """
    Wraps `post()` with exponential backoff + jitter on 429/5xx and transient errors.
    """
    attempt = 0
    last_err = None
    while attempt <= max_retries:
        try:
            return post(path, payload)
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            last_err = e
            if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                delay = (BASE_DELAY * (2 ** attempt)) + random.uniform(0, 0.25)
                logging.warning(f"Rate/Server limit hit ({status}) on {path}, retrying in {delay:.2f}s...")
                time.sleep(delay)
                attempt += 1
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                delay = (BASE_DELAY * (2 ** attempt)) + random.uniform(0, 0.25)
                logging.warning(f"Transient error on {path}: {e}; retrying in {delay:.2f}s...")
                time.sleep(delay)
                attempt += 1
                continue
            raise
    raise last_err

# Parameters -- /api/Order/place
# Name	Type	Description	Required	Nullable
# accountId integer The account ID. Required false
# contractId string The contract ID. Required false
# type integer The order type: 1=Limit, 2=Market, 4=Stop, 5=TrailingStop, 6=JoinBid, 7=JoinAsk
# side integer 0=Bid (buy), 1=Ask (sell)
# size integer The size
# limitPrice decimal Optional
# stopPrice decimal Optional
# trailPrice decimal Optional
# customTag string Optional
# linkedOrderId integer Optional

def place_market(acct_id, cid, side, size):
    logging.info("Placing market order acct=%s cid=%s side=%s size=%s", acct_id, cid, side, size)
    return _post_with_retry("/api/Order/place", {
        "accountId": acct_id, "contractId": cid,
        "type": 2, "side": side, "size": size
    })

def place_limit(acct_id, cid, side, size, px):
    logging.info("Placing limit order acct=%s cid=%s size=%s px=%s", acct_id, cid, size, px)
    return _post_with_retry("/api/Order/place", {
        "accountId": acct_id, "contractId": cid,
        "type": 1, "side": side, "size": size, "limitPrice": px
    })

def place_stop(acct_id, cid, side, size, px):
    logging.info("Placing stop order acct=%s cid=%s size=%s px=%s", acct_id, cid, size, px)
    return _post_with_retry("/api/Order/place", {
        "accountId": acct_id, "contractId": cid,
        "type": 4, "side": side, "size": size, "stopPrice": px
    })


def place_bracket_order(acct_id: int, cid: str, side: int, size: int, template_id: str,
                        time_in_force: str = "DAY") -> dict:
    """
    Submit a single server-side bracket order using a Topstep/ProjectX bracket template.

    This avoids any local SL/TP placement or polling and leaves risk controls to the
    template configured on the Topstep side.
    """
    if not template_id:
        raise ValueError("place_bracket_order requires a bracket template id")
    payload = {
        "accountId": acct_id,
        "contractId": cid,
        "side": side,
        "size": size,
        "bracketTemplateId": template_id,
        "timeInForce": time_in_force
    }
    logging.info(
        "Placing server-side bracket acct=%s cid=%s side=%s size=%s template=%s tif=%s",
        acct_id, cid, side, size, template_id, time_in_force
    )
    return _post_with_retry("/api/Order/placeBracket", payload)

# example of /api/Order/searchOpen return data
# { "orders":[{...}], "success": true, ... }
def search_open(acct_id, ttl: float = 5.0):
    """
    Open orders (cached briefly to avoid hammering). Uses retry/backoff.
    """
    key = f"orders_open:{acct_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    data = _post_with_retry("/api/Order/searchOpen", {"accountId": acct_id})
    orders = data.get("orders", [])
    logging.debug("Open orders for %s: %s", acct_id, orders)
    _cache_set(key, orders, ttl)
    return orders

# requires accountid and orderid
def cancel(acct_id, order_id):
    resp = _post_with_retry("/api/Order/cancel", {"accountId": acct_id, "orderId": order_id})
    if not resp.get("success", True):
        logging.warning("Cancel reported failure: %s", resp)
    return resp

# example of /api/Position/searchOpen return data
# { "positions":[{...}], "success": true, ... }
def search_pos(acct_id, ttl: float = 5.0):
    """
    Open positions (cached briefly + retry/backoff).
    """
    key = f"positions_open:{acct_id}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    data = _post_with_retry("/api/Position/searchOpen", {"accountId": acct_id})
    pos = data.get("positions", [])
    logging.debug("Open positions for %s: %s", acct_id, pos)
    _cache_set(key, pos, ttl)
    return pos

def search_accounts(only_active: bool = True):
    """
    Returns {account_id: {"name": str, "balance": float, "canTrade": bool, "isVisible": bool}}
    """
    ensure_token()
    payload = {"onlyActiveAccounts": bool(only_active)}
    data = _post_with_retry("/api/Account/search", payload)
    out = {}
    for a in data.get("accounts", []):
        out[a["id"]] = {
            "name": a.get("name"),
            "balance": a.get("balance"),
            "canTrade": a.get("canTrade"),
            "isVisible": a.get("isVisible"),
        }
    return out

def check_contract_rollover():
    """
    Check if contracts have rolled and update cache
    This should be called periodically (e.g., daily)
    """
    logging.info("Checking for contract rollover...")
    contract_cache.clear()
    contract_cache_expiry.clear()
    symbols = ["MES", "ES", "NQ", "MNQ"]
    changes = []
    for symbol in symbols:
        old_contract = contract_cache.get(f"{symbol}_False")
        new_contract = get_active_contract_for_symbol(symbol, live=False)
        if old_contract and new_contract and old_contract != new_contract:
            changes.append(f"{symbol}: {old_contract} -> {new_contract}")
            logging.warning(f"CONTRACT ROLLOVER DETECTED: {symbol} rolled from {old_contract} to {new_contract}")
    if changes:
        logging.warning(f"Contract rollovers detected: {', '.join(changes)}")
    return changes

# requires accountid and contractid
def close_pos(acct_id, cid):
    resp = _post_with_retry("/api/Position/closeContract", {"accountId": acct_id, "contractId": cid})
    if not resp.get("success", True):
        logging.warning("Close position reported failure: %s", resp)
    return resp

def search_trades(acct_id, since):
    trades = _post_with_retry("/api/Trade/search", {"accountId": acct_id, "startTimestamp": since.isoformat()}).get("trades", [])
    return trades

# custom flatten function
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

# custom cancel stops function
def cancel_all_stops(acct_id, cid):
    for o in search_open(acct_id):
        if o["contractId"] == cid and o["type"] == 4:
            cancel(acct_id, o["id"])

def search_contracts(search_text: str, live: bool = False) -> List[Dict]:
    """
    Search for contracts by name
    """
    try:
        resp = _post_with_retry("/api/Contract/search", {
            "searchText": search_text,
            "live": live
        })
        contracts = resp.get("contracts", [])
        logging.info(f"Found {len(contracts)} contracts for '{search_text}'")
        return contracts
    except Exception as e:
        logging.error(f"Error searching contracts: {e}")
        return []

def get_active_contract_for_symbol(symbol: str, live: bool = False) -> Optional[str]:
    """
    Get the active contract ID for a given symbol
    """
    contracts = search_contracts(symbol, live)
    active_contracts = [c for c in contracts if c.get("activeContract", False)]
    if not active_contracts:
        logging.warning(f"No active contracts found for {symbol}")
        return None
    active_contracts.sort(key=lambda x: x.get("id", ""))
    selected = active_contracts[0]
    logging.info(f"Selected active contract for {symbol}: {selected['id']} - {selected['description']}")
    return selected["id"]

# Update the get_contract function to support dynamic lookup
def get_contract(sym):
    """
    Get contract ID for a symbol. 
    """
    if OVERRIDE_CONTRACT_ID:
        return OVERRIDE_CONTRACT_ID
    if sym and sym.startswith("CON."):
        return sym
    if sym:
        base_symbol = sym.upper().replace("CON.F.US.", "").split(".")[0]
        live_mode = config.get('LIVE_MODE', False)
        contract_id = get_active_contract_for_symbol(base_symbol, live=live_mode)
        if contract_id:
            return contract_id
        else:
            logging.error(f"Could not find active contract for {base_symbol}")
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

# custom phantom order function
def check_for_phantom_orders(acct_id, cid):
    time.sleep(2)  # Give orders time to register
    positions = [p for p in search_pos(acct_id) if p["contractId"] == cid]
    open_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]
    if positions:
        has_protective = any(o["type"] in (1, 4) and o["status"] == 1 for o in open_orders)
        if not has_protective:
            time.sleep(3)
            open_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]
            has_protective = any(o["type"] in (1, 4) and o["status"] == 1 for o in open_orders)
        if not has_protective:
            logging.warning(f"Phantom position detected! No stop/limit attached. Positions: {positions}, Orders: {open_orders}")
            flatten_contract(acct_id, cid, timeout=10)
    else:
        phantom_orders = [
            o for o in open_orders
            if o.get("status") == 1 and o.get("type") in (1, 4)
        ]
        if phantom_orders:
            logging.warning(
                "Phantom protective orders detected without position! Contract %s, Orders: %s",
                cid,
                phantom_orders,
            )
            for order in phantom_orders:
                try:
                    cancel(acct_id, order["id"])
                except Exception as exc:
                    logging.error("Failed to cancel phantom order %s: %s", order.get("id"), exc)

# log results to supabase
def log_trade_results_to_supabase(acct_id, cid, entry_time, ai_decision_id, meta=None):
    import json
    import time
    from datetime import datetime, timedelta
    import logging

    meta = meta or {}

    # normalize entry_time to timezone-aware (Chicago)
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

    # First, let's get the entry order ID if we have it
    entry_order_id = meta.get("order_id")
    start_time = entry_time - timedelta(seconds=30)

    try:
        resp = _post_with_retry("/api/Trade/search", {
            "accountId": acct_id,
            "startTimestamp": start_time.isoformat()
        })
        all_trades = resp.get("trades", [])

        contract_trades = [
            t for t in all_trades
            if t.get("contractId") == cid and not t.get("voided", False)
        ]

        relevant_trades = []

        if entry_order_id:
            entry_trade = None
            for t in contract_trades:
                if t.get("orderId") == entry_order_id:
                    entry_trade = t
                    break
            if entry_trade:
                entry_trade_time = parser.isoparse(entry_trade["creationTimestamp"])
                for t in contract_trades:
                    trade_time = parser.isoparse(t["creationTimestamp"])
                    if t == entry_trade or (trade_time > entry_trade_time and t.get("profitAndLoss") is not None):
                        relevant_trades.append(t)
            else:
                logging.warning(f"Entry trade not found for order_id {entry_order_id}, using time-based approach")
                relevant_trades = get_trades_by_time_window(contract_trades, entry_time, exit_time)
        else:
            relevant_trades = get_trades_by_time_window(contract_trades, entry_time, exit_time)

        if not relevant_trades:
            logging.warning("[log_trade_results_to_supabase] No relevant trades found, skipping Supabase log.")
            return

        exit_trades = [t for t in relevant_trades if t.get("profitAndLoss") is not None]
        total_pnl = sum(float(t.get("profitAndLoss", 0)) for t in exit_trades)
        trade_ids = [t.get("id") for t in relevant_trades if t.get("id")]

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
    """
    relevant_trades = []
    if not contract_trades:
        return []
    sorted_trades = sorted(contract_trades, key=lambda t: t.get("creationTimestamp", ""))
    sessions = []
    current_session = []
    for i, trade in enumerate(sorted_trades):
        if i == 0:
            current_session.append(trade)
        else:
            prev_time = parser.isoparse(sorted_trades[i-1]["creationTimestamp"])
            curr_time = parser.isoparse(trade["creationTimestamp"])
            if (curr_time - prev_time).total_seconds() > 300:
                if current_session:
                    sessions.append(current_session)
                current_session = [trade]
            else:
                current_session.append(trade)
    if current_session:
        sessions.append(current_session)
    best_session = None
    best_time_diff = float('inf')
    for session in sessions:
        session_start = parser.isoparse(session[0]["creationTimestamp"])
        time_diff = abs((session_start - entry_time).total_seconds())
        if time_diff < best_time_diff and time_diff < 60:
            best_session = session
            best_time_diff = time_diff
    return best_session or []

def get_supabase_client() -> Client:
    url = SUPABASE_URL
    key = SUPABASE_KEY
    supabase: Client = create_client(url, key)
    return supabase

# regime updates
market_regime_analyzer = MarketRegime()
ohlc_regime_detector = OHLCRegimeDetector()

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
            logging.warning(f"🔄 REGIME CHANGE: {change_info['from']} → {change_info['to']} "
                            f"(lasted {duration:.1f} minutes)")
            return change_info
        return None

def fetch_multi_timeframe_analysis(n8n_base_url: str, timeframes: List[str] = None,
                                   cache_minutes: int = 4, force_refresh: bool = False) -> Dict:
    """
    Fetch multi-timeframe analysis using OHLC data only.

    TradingView alerts and AI consumers should rely on these aggregated arrays instead
    of screenshots or chart snapshots. This function keeps Supabase touches minimal
    and avoids any n8n/webhook calls when data is already present.
    """
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    supabase_client = get_supabase_client()

    if timeframes is None:
        timeframes = ['5m', '15m', '30m', '1h', '4h', '1D']

    request_key = hashlib.md5(f"{','.join(timeframes)}:{force_refresh}".encode()).hexdigest()
    with regime_cache_lock:
        regime_cache_in_progress[request_key] = True

    timeframe_minutes = {
        '5m': 5,
        '15m': 15,
        '30m': 30,
        '1h': 60,
        '4h': 240,
        '1D': 1440,
    }

    # Try to reuse recent cache from Supabase unless forced
    if not force_refresh:
        try:
            recent = supabase_client.table('market_regime_cache') \
                .select('*') \
                .gte('timestamp', (now - datetime.timedelta(minutes=cache_minutes)).isoformat()) \
                .order('timestamp', desc=True) \
                .limit(1) \
                .execute()
            if recent.data:
                rec = recent.data[0]
                cached_data = json.loads(rec.get('analysis_data', '{}'))
                if isinstance(cached_data, dict) and 'regime_analysis' in cached_data:
                    logging.info("Using cached OHLC regime analysis from Supabase")
                    with regime_cache_lock:
                        regime_cache_in_progress.pop(request_key, None)
                    return cached_data
        except Exception as e:
            logging.debug(f"Cache retrieval failed, proceeding to rebuild: {e}")

    ohlc_data: Dict[str, Dict] = {}

    try:
        logging.info("Fetching OHLC data from tv_datafeed (1m bars)")
        result = supabase_client.table('tv_datafeed') \
            .select('*') \
            .eq('symbol', 'MES') \
            .eq('timeframe', 1) \
            .order('ts', desc=True) \
            .limit(2000) \
            .execute()
        minute_bars = result.data or []
        for tf_name in timeframes:
            tf_minutes = timeframe_minutes.get(tf_name)
            if not tf_minutes:
                continue
            aggregated_bars = aggregate_1m_to_timeframe(minute_bars, tf_minutes)
            if len(aggregated_bars) < 10:
                logging.debug(f"Skipping {tf_name} due to insufficient bars ({len(aggregated_bars)})")
                continue
            ohlc_data[tf_name] = {
                'open': [bar['o'] for bar in aggregated_bars],
                'high': [bar['h'] for bar in aggregated_bars],
                'low': [bar['l'] for bar in aggregated_bars],
                'close': [bar['c'] for bar in aggregated_bars],
                'volume': [bar['v'] for bar in aggregated_bars],
                'rsi': [bar.get('rsi', 50) for bar in aggregated_bars],
                'macd_hist': [bar.get('macd_hist', 0) for bar in aggregated_bars],
                'atr': [bar.get('atr', 0) for bar in aggregated_bars],
                'fisher': [bar.get('fisher', 0) for bar in aggregated_bars],
                'vzo': [bar.get('vzo', 0) for bar in aggregated_bars],
                'phobos': [bar.get('phobos_momentum', 0) for bar in aggregated_bars],
                'stoch_k': [bar.get('stoch_k', 50) for bar in aggregated_bars],
                'bb_upper': [bar.get('bb_upper', bar['h']) for bar in aggregated_bars],
                'bb_middle': [bar.get('bb_middle', bar['c']) for bar in aggregated_bars],
                'bb_lower': [bar.get('bb_lower', bar['l']) for bar in aggregated_bars],
            }
        if not ohlc_data:
            logging.warning("No OHLC data aggregated; returning fallback regime")
            regime_analysis = market_regime_analyzer._get_fallback_regime("No OHLC data available")
        else:
            regime_analysis = ohlc_regime_detector.analyze_regime(ohlc_data)
    except Exception as e:
        logging.error(f"Critical error in fetch_multi_timeframe_analysis: {e}", exc_info=True)
        regime_analysis = market_regime_analyzer._get_fallback_regime(f"Critical error: {str(e)}")

    snapshot = {
        'timeframe_data': regime_analysis.get('timeframe_analysis', {}),
        'ohlc_data': ohlc_data,
        'regime_analysis': regime_analysis,
        'chart_urls': {},
        'timestamp': now.isoformat(),
        'analysis_method': 'ohlc_only'
    }

    try:
        supabase_client.table('market_regime_cache').insert({
            'analysis_data': json.dumps(snapshot),
            'timestamp': now.isoformat()
        }).execute()
    except Exception as e:
        logging.debug(f"Failed to persist regime cache (non-blocking): {e}")

    with regime_cache_lock:
        regime_cache_in_progress.pop(request_key, None)
    return snapshot

def evaluate_entry_quality(regime_analysis: Dict, position_context: Dict = None) -> Dict:
    """
    Evaluate entry quality based on hybrid analysis and position context
    """
    quality_score = 50
    factors = []
    confidence = regime_analysis.get('confidence', 0)
    if confidence > 80:
        quality_score += 15
        factors.append("High confidence setup")
    elif confidence < 60:
        quality_score -= 10
        factors.append("Low confidence")
    regime = regime_analysis.get('primary_regime')
    if regime in ['trending_up', 'trending_down']:
        quality_score += 10
        factors.append("Trending market")
    elif regime == 'choppy':
        quality_score -= 15
        factors.append("Choppy conditions")
    elif regime == 'ranging':
        quality_score += 5
        factors.append("Range-bound (fade extremes)")
    trend_details = regime_analysis.get('trend_details', {})
    alignment = trend_details.get('alignment_score', 0)
    if alignment:
        if alignment > 80:
            quality_score += 5
            factors.append(f"Strong alignment ({alignment:.0f}%)")
        elif alignment < 50:
            quality_score -= 5
            factors.append(f"Weak alignment ({alignment:.0f}%)")
    if position_context:
        if not position_context.get('account_metrics', {}).get('can_trade', True):
            quality_score = 0
            factors.append("Account cannot trade")
        risk_level = position_context.get('account_metrics', {}).get('risk_level', 'medium')
        if risk_level == 'high':
            quality_score -= 20
            factors.append("High account risk")
        if position_context.get('current_position', {}).get('has_position'):
            quality_score -= 10
            factors.append("Already in position")
    quality_score = max(0, min(100, quality_score))
    if quality_score >= 70:
        recommendation = 'STRONG_ENTRY'
    elif quality_score >= 60:
        recommendation = 'ENTRY_OK'
    elif quality_score >= 50:
        recommendation = 'WEAK_ENTRY'
    else:
        recommendation = 'NO_ENTRY'
    return {
        'quality_score': quality_score,
        'factors': factors,
        'recommendation': recommendation,
        'regime': regime,
        'confidence': confidence
    }

def ai_trade_decision_with_regime(account, strat, action, sym, size, alert, ai_url):
    """
    Enhanced AI trade decision that includes OHLC-only regime analysis and position context.

    The AI is expected to return only high-level intents (action/intent/size/bracket_template)
    without any SL/TP geometry.
    """
    try:
        market_analysis = fetch_multi_timeframe_analysis(ai_url, force_refresh=False)
        regime = market_analysis['regime_analysis']
        regime_rules = market_regime_analyzer.get_regime_trading_rules(regime['primary_regime'])
        ohlc_data = market_analysis.get('ohlc_data', {})
        acct_id = ACCOUNTS.get(account.lower())
        if acct_id:
            from position_manager import PositionManager
            pm = PositionManager(ACCOUNTS)
            cid = get_contract(sym)
            position_context = pm.get_position_context_for_ai(acct_id, cid)
            if not position_context['account_metrics']['can_trade']:
                logging.warning(f"Account {account} cannot trade due to risk limits")
                return {
                    "strategy": strat,
                    "action": "HOLD",
                    "account": account,
                    "reason": f"Account risk limits exceeded: {', '.join(position_context['warnings'])}",
                    "regime": regime['primary_regime'],
                    "regime_confidence": regime.get('confidence', 0),
                    "error": False
                }
        else:
            position_context = None

        entry_quality = evaluate_entry_quality(regime, position_context)
        logging.info(f"Entry quality: {entry_quality['quality_score']}/100 - {entry_quality['recommendation']}")
        logging.info(f"Quality factors: {', '.join(entry_quality['factors'])}")
        if entry_quality['recommendation'] == 'NO_ENTRY' and entry_quality['quality_score'] < 40:
            logging.warning(f"Blocking trade due to poor entry quality ({entry_quality['quality_score']})")
            return {
                "strategy": strat,
                "action": "HOLD",
                "account": account,
                "reason": f"Poor entry quality: {', '.join(entry_quality['factors'])}",
                "regime": regime['primary_regime'],
                "regime_confidence": regime.get('confidence', 0),
                "entry_quality": entry_quality,
                "error": False
            }
        if not regime['trade_recommendation']:
            logging.warning(f"Trading not recommended in {regime['primary_regime']} regime. Blocking trade.")
            return {
                "strategy": strat,
                "action": "HOLD",
                "account": account,
                "reason": f"Market regime ({regime['primary_regime']}) not suitable for trading. {', '.join(regime['supporting_factors'])}",
                "regime": regime['primary_regime'],
                "regime_confidence": regime.get('confidence', 0),
                "error": False
            }
        if regime_rules['avoid_signal'] == 'BOTH' or (regime_rules['avoid_signal'] and action == regime_rules['avoid_signal']):
            logging.warning(f"Action {action} conflicts with {regime['primary_regime']} regime preferences")
            return {
                "strategy": strat,
                "action": "HOLD",
                "account": account,
                "reason": f"{action} action not recommended in {regime['primary_regime']} regime",
                "regime": regime['primary_regime'],
                "regime_confidence": regime.get('confidence', 0),
                "error": False
            }

        payload = {
            "account": account,
            "strategy": strat,
            "action": action,
            "symbol": sym,
            "size": size,
            "alert": alert,
            "ohlc_data": ohlc_data,
            "market_analysis": {
                "regime": regime['primary_regime'],
                "confidence": regime.get('confidence', 0),
                "supporting_factors": regime.get('supporting_factors', []),
                "risk_level": regime.get('risk_level'),
                "trend_details": regime.get('trend_details', {}),
                "volatility_details": regime.get('volatility_details', {}),
                "momentum_details": regime.get('momentum_details', {})
            },
            "regime_rules": regime_rules,
            "timeframe_signals": {
                tf: data.get('signal', 'HOLD')
                for tf, data in market_analysis.get('timeframe_data', {}).items()
            },
            "entry_quality": entry_quality
        }
        if position_context:
            payload["position_context"] = position_context
            if position_context['current_position']['has_position']:
                current_side = position_context['current_position']['side']
                if (current_side == 'LONG' and action == 'SELL') or (current_side == 'SHORT' and action == 'BUY'):
                    payload['position_warning'] = f"Action would reverse current {current_side} position"
                if position_context['current_position']['size'] >= 3:
                    payload['suggested_size'] = 0
                elif position_context['account_metrics']['risk_level'] == 'high':
                    payload['suggested_size'] = 1
                else:
                    payload['suggested_size'] = size

        resp = session.post(ai_url, json=payload, timeout=240)
        resp.raise_for_status()
        raw_body = resp.text.strip() if resp.text is not None else ""
        if not raw_body:
            logging.warning("AI response empty body; defaulting to HOLD decision")
            return {
                "strategy": strat,
                "action": "HOLD",
                "account": account,
                "reason": "AI response was empty",
                "regime": regime['primary_regime'],
                "regime_confidence": regime.get('confidence', 0),
                "entry_quality": entry_quality,
                "error": False
            }
        try:
            data = resp.json()
        except Exception as e:
            logging.error(f"AI response not valid JSON: {resp.text}")
            return {
                "strategy": strat,
                "action": "HOLD",
                "account": account,
                "reason": f"AI response parsing error: {str(e)}",
                "regime": regime['primary_regime'],
                "regime_confidence": regime.get('confidence', 0),
                "entry_quality": entry_quality,
                "error": True
            }

        clean_action = (data.get("action") or data.get("signal") or "HOLD").upper()
        clean_intent = data.get("intent") or data.get("position_action")
        clean_size = data.get("size", size)
        decision = {
            "strategy": strat,
            "action": clean_action,
            "intent": clean_intent,
            "size": clean_size,
            "symbol": data.get("symbol", sym),
            "alert": data.get("alert", alert),
            "account": account,
            "reason": data.get("reason"),
            "regime": regime['primary_regime'],
            "regime_confidence": regime.get('confidence', 0),
            "entry_quality": entry_quality,
            "ai_decision_id": data.get("ai_decision_id"),
            "bracket_template": data.get("bracket_template")
        }
        if regime_rules['max_position_size'] > 0:
            decision['size'] = min(decision['size'], regime_rules['max_position_size'])
            if position_context and 'suggested_size' in payload:
                decision['size'] = min(decision['size'], payload['suggested_size'])
        return decision
    except Exception as e:
        logging.error(f"AI error with regime analysis: {str(e)}")
        return {
            "strategy": strat,
            "action": "HOLD",
            "account": account,
            "reason": f"AI error: {str(e)}",
            "regime": "unknown",
            "regime_confidence": 0,
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
        cid = get_contract('MES')
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
        n8n_ai_url = config.get('N8N_AI_URL', '')
        if '/webhook/' in n8n_ai_url:
            n8n_base_url = n8n_ai_url.split('/webhook/')[0]
        else:
            n8n_base_url = n8n_ai_url.replace('/webhook', '')
        market_analysis = fetch_multi_timeframe_analysis(n8n_base_url, force_refresh=force_refresh)
        if isinstance(market_analysis, list):
            if market_analysis:
                market_analysis = market_analysis[0]
            else:
                raise ValueError("Empty market analysis list")
        if not isinstance(market_analysis, dict):
            raise ValueError(f"Unexpected market_analysis type: {type(market_analysis)}")
        regime = market_analysis.get('regime_analysis', {})
        if not regime:
            raise ValueError("No regime_analysis in market data")
        trend_details = regime.get('trend_details', {})
        volatility_details = regime.get('volatility_details', {})
        summary = {
            'timestamp': datetime.now(CT).isoformat(),
            'regime': regime.get('primary_regime', 'unknown'),
            'confidence': regime.get('confidence', 0),
            'trade_recommended': regime.get('trade_recommendation', False),
            'risk_level': regime.get('risk_level', 'high'),
            'key_factors': regime.get('supporting_factors', ['Error in analysis'])[:3],
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
                bar_time = parser.parse(record.get('ts'))
                current_time = datetime.now(timezone.utc)
                age_seconds = (current_time - bar_time).total_seconds()
                if age_seconds <= max_age_seconds:
                    logging.debug(f"Current price from 1m feed: ${price} (age: {age_seconds:.0f}s)")
                    return price, f"1m_feed_{int(age_seconds)}s_old"
                else:
                    logging.debug(f"1m data too old: {age_seconds:.0f}s > {max_age_seconds}s")
        except Exception as e:
            logging.error(f"Error querying tv_datafeed: {e}")
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
                timestamp = parser.parse(record.get('timestamp'))
                age_seconds = (datetime.now(timezone.utc) - timestamp).total_seconds()
                if age_seconds <= 360:
                    snapshot = record.get('snapshot')
                    if isinstance(snapshot, str):
                        snapshot = json.loads(snapshot)
                    price = snapshot.get('current_price')
                    if price:
                        logging.debug(f"Current price from 5m chart: ${price} (age: {age_seconds:.0f}s)")
                        return float(price), f"5m_chart_{int(age_seconds)}s_old"
        except Exception as e:
            logging.debug(f"Could not get chart price: {e}")
        now = datetime.now(CT)
        is_market_closed = (
            now.weekday() == 5 or
            (now.weekday() == 6 and now.hour < 17) or
            (now.weekday() == 4 and now.hour >= 16)
        )
        if is_market_closed:
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
    """
    try:
        supabase = get_supabase_client()
        result = supabase.table('tv_datafeed') \
            .select('o, h, l, c, ts') \
            .eq('symbol', 'MES') \
            .eq('timeframe', 1) \
            .order('ts', desc=True) \
            .limit(1) \
            .execute()
        if result.data and len(result.data) > 0:
            bar = result.data[0]
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
        bars_needed = {
            '5m': 50,
            '15m': 50,
            '30m': 30
        }
        ohlc_data = {}
        for timeframe, bars in bars_needed.items():
            tf_minutes = int(timeframe.replace('m', ''))
            result = supabase.table('tv_datafeed') \
                .select('o, h, l, c, v, ts') \
                .eq('symbol', symbol) \
                .eq('timeframe', tf_minutes) \
                .order('ts', desc=True) \
                .limit(bars) \
                .execute()
            if result.data and len(result.data) > 10:
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

def aggregate_1m_to_timeframe(minute_bars: List[Dict], target_minutes: int) -> List[Dict]:
    """
    Efficiently aggregate 1-minute bars to target timeframe
    """
    if not minute_bars or len(minute_bars) < target_minutes:
        return []
    sorted_bars = sorted(minute_bars, key=lambda x: x['ts'])
    aggregated = []
    for i in range(0, len(sorted_bars) - target_minutes + 1, target_minutes):
        chunk = sorted_bars[i:i + target_minutes]
        if len(chunk) == target_minutes:
            agg_bar = {
                'ts': chunk[-1]['ts'],
                'o': float(chunk[0]['o']),
                'h': max(float(bar['h']) for bar in chunk),
                'l': min(float(bar['l']) for bar in chunk),
                'c': float(chunk[-1]['c']),
                'v': sum(float(bar.get('v', 0)) for bar in chunk),
                'rsi': float(chunk[-1].get('rsi', 50)),
                'macd_hist': float(chunk[-1].get('macd_hist', 0)),
                'atr': float(chunk[-1].get('atr', 10)),
                'fisher': float(chunk[-1].get('fisher', 0)),
                'vzo': float(chunk[-1].get('vzo', 0)),
                'phobos_momentum': float(chunk[-1].get('phobos_momentum', 0)),
                'stoch_k': float(chunk[-1].get('stoch_k', 50)),
                'bb_upper': float(chunk[-1].get('bb_upper', 0)),
                'bb_middle': float(chunk[-1].get('bb_middle', 0)),
                'bb_lower': float(chunk[-1].get('bb_lower', 0))
            }
            aggregated.append(agg_bar)
    return aggregated[-50:] if len(aggregated) > 50 else aggregated
