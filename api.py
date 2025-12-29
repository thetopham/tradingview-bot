# api.py
import requests
import logging
import json
import time
import pytz
import threading
import hashlib
import random  # NEW: for jitter in backoff
import pandas as pd
import numpy as np

from datetime import datetime, timezone, timedelta
from auth import ensure_token, get_token
from config import load_config
from dateutil import parser
from market_state import MarketStateConfig, RollingFiveMinuteEngine, compute_market_state
from confluence import compute_confluence
from adaptive_confluence import AdaptiveConfluenceParams
from supabase import create_client, Client
from typing import Dict, List, Optional, Tuple
from threading import RLock

# ─────────────────────────────
# Tiny in-memory TTL cache (already present)
# ─────────────────────────────
_CACHE = {}
_CACHE_LOCK = RLock()
_ENGINE_LOCK = RLock()
_rolling_engine: Optional[RollingFiveMinuteEngine] = None
_insufficient_bars_logged = False
_ADAPTIVE_LOCK = RLock()
_adaptive_params: Optional[AdaptiveConfluenceParams] = None
_adaptive_update_counter = 0
_ADAPTIVE_SAVE_FREQUENCY = 10

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


def _get_engine(bars_needed: int) -> RollingFiveMinuteEngine:
    global _rolling_engine
    with _ENGINE_LOCK:
        if _rolling_engine is None:
            _rolling_engine = RollingFiveMinuteEngine(bars_needed=bars_needed)
        else:
            _rolling_engine.bars_needed = max(_rolling_engine.bars_needed, bars_needed)
        return _rolling_engine


def _get_adaptive_params() -> AdaptiveConfluenceParams:
    global _adaptive_params
    with _ADAPTIVE_LOCK:
        if _adaptive_params is None:
            _adaptive_params = AdaptiveConfluenceParams.load()
        return _adaptive_params


def _compute_z_ema21_series(df: pd.DataFrame, lookback: int) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=float)

    work = df.copy()
    if "ts" in work.columns:
        work = work.sort_values("ts")

    work = work.tail(max(lookback, 50))
    closes = work["c"].astype(float)

    ema_series = work.get("ema21")
    if ema_series is None or ema_series.isna().all():
        ema_series = closes.ewm(span=21, adjust=False).mean()
    else:
        ema_series = ema_series.astype(float)

    atr_series = work.get("atr")
    if atr_series is None or atr_series.isna().all():
        highs = work["h"].astype(float)
        lows = work["l"].astype(float)
        prev_close = closes.shift(1)
        tr = pd.concat(
            [
                (highs - lows).abs(),
                (highs - prev_close).abs(),
                (lows - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr_series = tr.rolling(window=14, min_periods=14).mean()
    else:
        atr_series = atr_series.astype(float)

    with np.errstate(divide="ignore", invalid="ignore"):
        z_series = (closes - ema_series) / atr_series
    z_series = z_series.replace([np.inf, -np.inf], np.nan).dropna()
    return z_series.tail(lookback)


def _update_adaptive_params(ohlc5m_df: pd.DataFrame, market_state: Dict) -> Tuple[AdaptiveConfluenceParams, int]:
    global _adaptive_update_counter
    with _ADAPTIVE_LOCK:
        params = _get_adaptive_params()
        z_series = _compute_z_ema21_series(ohlc5m_df, params.n)
        sample_count = len(z_series)
        side = (market_state or {}).get("signal")
        if side in {"BUY", "SELL"}:
            updated = params.update_from_series(z_series, side)
            if updated:
                _adaptive_update_counter += 1
                if _adaptive_update_counter % _ADAPTIVE_SAVE_FREQUENCY == 0:
                    params.save()
        return params, sample_count


def _log_adaptive_snapshot(params: AdaptiveConfluenceParams, sample_count: int) -> None:
    logging.info(
        "Adaptive params snapshot: sell_zone=%s buy_zone=%s threshold=%.3f samples=%d",
        params.sell_zone,
        params.buy_zone,
        params.threshold,
        sample_count,
    )

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


def _timeframe_filters(minutes: int) -> List:
    """Return accepted Supabase timeframe representations for the given interval."""
    return [minutes, str(minutes), f"{minutes}m"]


INDICATOR_FIELDS = [
    "ema21",
    "vwap",
    "atr",
    "bb_upper",
    "bb_lower",
    "bb_middle",
    "rsi",
    "macd",
    "macd_signal",
    "macd_hist",
]


def _aggregate_1m_bars_to_5m(bars: List[Dict]) -> List[Dict]:
    """Aggregate 1m bars into 5m bars, ordered from oldest to newest."""

    def _merge_bucket(bucket_start, bucket_bars):
        merged = {
            'o': float(bucket_bars[0]['o']),
            'h': max(float(b['h']) for b in bucket_bars),
            'l': min(float(b['l']) for b in bucket_bars),
            'c': float(bucket_bars[-1]['c']),
            'v': sum(float(b.get('v', 0)) for b in bucket_bars),
            'ts': bucket_start.isoformat()
        }

        for field in INDICATOR_FIELDS:
            value = next((b.get(field) for b in reversed(bucket_bars) if field in b), None)
            if value is not None:
                try:
                    merged[field] = float(value)
                except Exception:
                    merged[field] = value

        return merged

    aggregated: List[Dict] = []
    current_bucket_start = None
    bucket: List[Dict] = []

    # Sort chronologically to build buckets
    sorted_bars = sorted(
        [b for b in bars if b.get('ts')], key=lambda b: parser.parse(b['ts'])
    )

    for bar in sorted_bars:
        try:
            ts = parser.parse(bar['ts'])
        except Exception as e:
            logging.debug("Skipping 1m bar with invalid ts %s: %s", bar.get('ts'), e)
            continue
        bucket_start = ts - timedelta(
            minutes=ts.minute % 5, seconds=ts.second, microseconds=ts.microsecond
        )
        if current_bucket_start is None:
            current_bucket_start = bucket_start
        if bucket_start != current_bucket_start:
            if bucket:
                aggregated.append(_merge_bucket(current_bucket_start, bucket))
            bucket = []
            current_bucket_start = bucket_start
        bucket.append(bar)

    if bucket:
        aggregated.append(_merge_bucket(current_bucket_start, bucket))

    return aggregated


def _fetch_5m_bars_from_1m(supabase: Client, symbol: str, bars_needed: int) -> List[Dict]:
    """Fetch 1m bars and aggregate into 5m bars for Supabase-backed datafeed."""

    minute_bars_needed = (bars_needed * 5) + 5  # pad for partial bucket
    result = (
        supabase
        .table('tv_datafeed')
        .select('o, h, l, c, v, ts, ' + ', '.join(INDICATOR_FIELDS))
        .eq('symbol', symbol)
        .in_('timeframe', _timeframe_filters(1))
        .order('ts', desc=True)
        .limit(minute_bars_needed)
        .execute()
    )
    minute_bars = result.data or []
    if not minute_bars:
        logging.warning("No 1m bars returned from tv_datafeed for %s", symbol)
        return []

    five_minute_bars = _aggregate_1m_bars_to_5m(minute_bars)
    if len(five_minute_bars) > bars_needed:
        five_minute_bars = five_minute_bars[-bars_needed:]
    if len(five_minute_bars) < bars_needed:
        logging.warning(
            "Only %s of %s requested 5m bars available after aggregation for %s",
            len(five_minute_bars), bars_needed, symbol,
        )
    return five_minute_bars


def _cache_market_bars(symbol: str, five_minute_bars: List[Dict], ttl: float = 1800) -> None:
    _cache_set(f"market_bars:{symbol}:5m", five_minute_bars, ttl)


def _build_market_summary(market_state: Dict) -> Dict:
    summary = {
        'timestamp': datetime.now(CT).isoformat(),
        'regime': market_state.get('regime', 'sideways'),
        'trend': market_state.get('trend', 'sideways'),
        'confidence': market_state.get('confidence', 0),
        'trade_recommended': market_state.get('signal') in ('BUY', 'SELL'),
        'risk_level': 'medium',
        'key_factors': market_state.get('supporting_factors', ['Local analysis unavailable'])[:3],
        'trend_alignment': market_state.get('slope_norm', 0),
        'volatility': 'unknown'
    }
    summary['market_state'] = market_state
    return summary


def _log_market_state_to_supabase(summary: Dict, symbol: str = "MES", timeframe: str = "5m") -> None:
    """Persist the latest 5m market state/conditions snapshot to Supabase."""

    if not SUPABASE_URL or not SUPABASE_KEY:
        logging.debug("Supabase credentials missing; skipping market state log")
        return

    snapshot = {
        'symbol': symbol,
        'timeframe': timeframe,
        'market_state': summary.get('market_state', {}),
        'conditions': {k: v for k, v in summary.items() if k != 'market_state'},
    }
    record = {
        'symbol': symbol,
        'timeframe': timeframe,
        'snapshot': snapshot,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'source': 'local_market_state',
    }

    try:
        supabase = get_supabase_client()

        # Historical record
        supabase.table('chart_analysis').insert(record).execute()

        # Latest pointer for dashboards/consumers
        supabase.table('latest_chart_analysis').upsert(
            record,
            on_conflict="symbol,timeframe",
        ).execute()

        logging.info("Logged 5m market state snapshot to Supabase for %s", symbol)
    except Exception as exc:
        logging.error("Failed to log market state to Supabase: %s", exc)


def _apply_confluence(summary: Dict, bars: List[Dict], market_state: Dict) -> Dict:
    try:
        ohlc5m_df = pd.DataFrame(bars)
        params, sample_count = _update_adaptive_params(ohlc5m_df, market_state)
        confluence = compute_confluence(
            ohlc5m_df,
            base_signal=market_state.get('signal'),
            market_state=market_state,
            params=params,
        )
        summary.update(confluence)
        _log_adaptive_snapshot(params, sample_count)

        confluence_tags = []
        conf_obj = summary.get('confluence', {})
        for comp in conf_obj.get('components', []):
            confluence_tags.extend([t for t in comp.get('tags', []) if t])
        for gate_name, gate_val in (conf_obj.get('gates') or {}).items():
            confluence_tags.append(gate_name if gate_val else f"{gate_name}_blocked")
        if conf_obj.get('trade_recommended'):
            confluence_tags.append(f"bias_{conf_obj.get('bias', 'HOLD').lower()}")

        if confluence_tags:
            existing = summary.get('key_factors', [])
            summary['key_factors'] = (existing + confluence_tags)[:6]
    except Exception as exc:
        logging.error("Failed to compute confluence: %s", exc)
    return summary

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

def get_market_conditions_summary(
    force_refresh: bool = False,
    symbol: str = "MES",
    bars_needed: int = 90,
    cached_only: bool = False,
) -> Dict:
    """Get a summary of current market conditions for logging with TTL cache."""

    cache_key = f"market_summary:{symbol}:5m"
    provisional_key = f"{cache_key}:provisional"

    try:
        if not force_refresh:
            cached = _cache_get(cache_key)
            if cached:
                logging.info("Market summary cache hit for %s", symbol)
                return cached

            if cached_only:
                provisional = _cache_get(provisional_key)
                if provisional:
                    logging.info("Using provisional cached market summary for %s", symbol)
                    return provisional
                logging.info(
                    "No cached market summary for %s; skipping recompute (cached_only)",
                    symbol,
                )
                return _build_market_summary(compute_market_state([]))

        supabase = get_supabase_client()
        bars = _fetch_5m_bars_from_1m(
            supabase, symbol or config.get('DEFAULT_SYMBOL', 'MES'), bars_needed=bars_needed
        )
        if not bars:
            logging.warning("No 5m bars available after aggregating 1m feed")

        market_state = compute_market_state(bars)
        summary = _build_market_summary(market_state)
        summary = _apply_confluence(summary, bars, market_state)

        ttl = random.uniform(240, 290)
        _cache_set(cache_key, summary, ttl)
        _cache_set(provisional_key, summary, ttl)
        _cache_market_bars(symbol, bars)
        _log_market_state_to_supabase(summary, symbol=symbol, timeframe="5m")

        engine = _get_engine(bars_needed)
        engine.prime(bars)

        logging.info("Market summary recomputed for %s; cached for %ds", symbol, int(ttl))
        logging.info(f"Market Conditions: {summary}")
        return summary
    except Exception as e:
        logging.error(f"Error getting market conditions: {e}")
        return {
            'timestamp': datetime.now(CT).isoformat(),
            'regime': 'sideways',
            'trend': 'sideways',
            'confidence': 0,
            'trade_recommended': False,
            'risk_level': 'high',
            'key_factors': ['Error in analysis'],
            'trend_alignment': 0,
            'volatility': 'unknown'
        }


def update_market_state_incremental(symbol: str = "MES", bars_needed: int = 90) -> Optional[Dict]:
    """Update market state cache using only the latest 1m bar."""

    cache_key = f"market_summary:{symbol}:5m"
    provisional_key = f"{cache_key}:provisional"
    bars_cache_key = f"market_bars:{symbol}:5m"
    required_bars = MarketStateConfig().slope_lookback + 2

    try:
        engine = _get_engine(bars_needed)
        if not engine.has_history():
            cached_bars = _cache_get(bars_cache_key)
            if cached_bars:
                engine.prime(cached_bars)

        supabase = get_supabase_client()
        result = (
            supabase
            .table('tv_datafeed')
            .select('o, h, l, c, v, ts')
            .eq('symbol', symbol)
            .in_('timeframe', _timeframe_filters(1))
            .order('ts', desc=True)
            .limit(1)
            .execute()
        )

        latest = (result.data or [None])[0]
        if not latest or not latest.get('ts'):
            return None

        prev_ts = engine.last_1m_ts
        completed_bar = engine.ingest_1m_bar(latest)

        if engine.last_1m_ts == prev_ts:
            logging.debug("No new 1m bar for %s; skipping incremental update", symbol)
            return None

        bars_with_partial = engine.get_bars(include_partial=True)
        if len(bars_with_partial) < required_bars:
            global _insufficient_bars_logged
            if not _insufficient_bars_logged:
                logging.debug(
                    "Not enough bars (%s/%s) for incremental market state; waiting for more.",
                    len(bars_with_partial),
                    required_bars,
                )
                _insufficient_bars_logged = True
            return None

        provisional_state = compute_market_state(bars_with_partial)
        provisional_summary = _build_market_summary(provisional_state)
        provisional_summary = _apply_confluence(provisional_summary, bars_with_partial, provisional_state)
        _cache_set(provisional_key, provisional_summary, ttl=180)

        if completed_bar:
            bars_without_partial = engine.get_bars(include_partial=False)
            if len(bars_without_partial) < required_bars:
                return provisional_summary
            final_state = compute_market_state(bars_without_partial)
            final_summary = _build_market_summary(final_state)
            final_summary = _apply_confluence(final_summary, bars_without_partial, final_state)
            ttl = random.uniform(240, 290)
            _cache_set(cache_key, final_summary, ttl)
            _cache_set(provisional_key, final_summary, ttl)
            _cache_market_bars(symbol, engine.get_bars(include_partial=False))
            _log_market_state_to_supabase(final_summary, symbol=symbol, timeframe="5m")
            logging.info("Incremental 5m close cached for %s; ttl=%ds", symbol, int(ttl))
        else:
            logging.debug("Cached provisional market summary for %s", symbol)

        return provisional_summary
    except Exception as e:
        logging.error(f"Error during incremental market update: {e}")
        return None

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
                .in_('timeframe', _timeframe_filters(1)) \
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
                    .in_('timeframe', _timeframe_filters(1)) \
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
            .in_('timeframe', _timeframe_filters(1)) \
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
            '5m': 50
        }
        ohlc_data = {}
        for timeframe, bars in bars_needed.items():
            if timeframe == '5m':
                data = _fetch_5m_bars_from_1m(supabase, symbol, bars_needed=bars)
            else:
                tf_minutes = int(timeframe.replace('m', ''))
                result = supabase.table('tv_datafeed') \
                    .select('o, h, l, c, v, ts') \
                    .eq('symbol', symbol) \
                    .in_('timeframe', _timeframe_filters(tf_minutes)) \
                    .order('ts', desc=True) \
                    .limit(bars) \
                    .execute()
                data = list(reversed(result.data)) if result.data else []

            if data and len(data) > 10:
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
