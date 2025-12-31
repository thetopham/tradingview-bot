# api.py
import requests
import logging
import json
import time
import pytz
from typing import Dict, List, Optional, Tuple

from datetime import datetime, timezone
from auth import ensure_token, get_token, session
from config import load_config
from dateutil import parser
from supabase import create_client


config = load_config()
OVERRIDE_CONTRACT_ID = config['OVERRIDE_CONTRACT_ID']
PX_BASE = config['PX_BASE']
SUPABASE_URL = config['SUPABASE_URL']
SUPABASE_KEY = config['SUPABASE_KEY']
CT = pytz.timezone("America/Chicago")
MES = "MES"

_PRICE_CACHE: Dict[str, Optional[Tuple[float, str]]] = {
    "symbol": None,
    "ts": 0,
    "value": None,
}
_SUPABASE_CLIENT = None


def _timeframe_filters(max_minutes: int = 1) -> List[str]:
    """Return timeframes up to the requested minute window (defaults to 1m)."""

    try:
        window = int(max_minutes)
    except Exception:
        window = 1

    if window <= 1:
        return ["1m"]
    if window <= 5:
        return ["1m", "5m"]
    return ["1m", "5m", "15m"]


def get_supabase_client():
    global _SUPABASE_CLIENT
    if _SUPABASE_CLIENT is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError("Supabase credentials are not configured")
        _SUPABASE_CLIENT = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _SUPABASE_CLIENT



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

def place_market_bracket(acct_id, cid, side, size, *, stop_loss_ticks=None, take_profit_ticks=None, custom_tag=None):
    payload = {
        "accountId": acct_id,
        "contractId": cid,
        "type": 2,
        "side": side,
        "size": size,
        "customTag": custom_tag
    }

    if stop_loss_ticks is not None:
        payload["stopLossBracket"] = {"ticks": int(stop_loss_ticks), "type": 4}
    if take_profit_ticks is not None:
        payload["takeProfitBracket"] = {"ticks": int(take_profit_ticks), "type": 1}

    logging.info(
        "Placing bracket market order acct=%s cid=%s side=%s size=%s sl_ticks=%s tp_ticks=%s",
        acct_id, cid, side, size, stop_loss_ticks, take_profit_ticks,
    )
    return post("/api/Order/place", payload)

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

def _summarize_positions(positions, timeframe: str = "1m"):
    summary = []
    for pos in positions:
        side_code = pos.get("type")
        side = "LONG" if side_code == 1 else "SHORT" if side_code == 2 else "UNKNOWN"
        size = pos.get("size") or 0
        cid = pos.get("contractId") or pos.get("contractSymbol")
        avg_price = pos.get("avgPrice") or pos.get("averagePrice") or pos.get("entryPrice")

        symbol = MES
        current_price = _fetch_latest_price_from_supabase(symbol, timeframe) if symbol else None

        if current_price is None or avg_price is None:
            pnl = None
        elif side == "LONG":
            pnl = (current_price - avg_price) * size
        elif side == "SHORT":
            pnl = (avg_price - current_price) * size
        else:
            pnl = None

        details = {
            "contract": cid,
            "side": side,
            "size": size,
            "avg_price": avg_price,
            "pnl": pnl,
        }
        summary.append(details)
    return summary

def get_current_market_price(symbol: str = "MES", max_age_seconds: int = 120) -> Tuple[Optional[float], Optional[str]]:
    """Get the current market price from Supabase sources.

    Falls back gracefully between 1m tv_datafeed bars and latest_chart_analysis,
    and caches the last value for a few seconds to avoid repeated lookups.
    """

    try:
        now_ts = time.time()
        if (
            _PRICE_CACHE.get("symbol") == symbol
            and now_ts - _PRICE_CACHE.get("ts", 0) <= 10
            and _PRICE_CACHE.get("value")
        ):
            price, source = _PRICE_CACHE.get("value")
            return price, source

        try:
            supabase = get_supabase_client()
        except Exception as exc:
            logging.error("Supabase client unavailable for price lookup: %s", exc)
            supabase = None

        if supabase:
            try:
                result = (
                    supabase
                    .table('tv_datafeed')
                    .select('c, ts')
                    .eq('symbol', symbol)
                    .in_('timeframe', _timeframe_filters(1))
                    .order('ts', desc=True)
                    .limit(1)
                    .execute()
                )
                if result.data:
                    record = result.data[0]
                    price = float(record.get('c'))
                    bar_time = parser.parse(record.get('ts'))
                    current_time = datetime.now(timezone.utc)
                    age_seconds = (current_time - bar_time).total_seconds()
                    if age_seconds <= max_age_seconds:
                        logging.debug("Current price from 1m feed: $%s (age: %.0fs)", price, age_seconds)
                        _PRICE_CACHE.update({"symbol": symbol, "ts": now_ts, "value": (price, f"1m_feed_{int(age_seconds)}s_old")})
                        return price, f"1m_feed_{int(age_seconds)}s_old"
                    logging.debug("1m data too old: %.0fs > %ss", age_seconds, max_age_seconds)
            except Exception as e:
                logging.error("Error querying tv_datafeed: %s", e)

            try:
                result = (
                    supabase
                    .table('latest_chart_analysis')
                    .select('snapshot, timestamp')
                    .eq('symbol', symbol)
                    .eq('timeframe', '5m')
                    .order('timestamp', desc=True)
                    .limit(1)
                    .execute()
                )
                if result.data:
                    record = result.data[0]
                    timestamp = parser.parse(record.get('timestamp'))
                    age_seconds = (datetime.now(timezone.utc) - timestamp).total_seconds()
                    if age_seconds <= 360:
                        snapshot = record.get('snapshot')
                        if isinstance(snapshot, str):
                            snapshot = json.loads(snapshot)
                        price = snapshot.get('current_price')
                        if price:
                            logging.debug("Current price from 5m chart: $%s (age: %.0fs)", price, age_seconds)
                            _PRICE_CACHE.update({"symbol": symbol, "ts": now_ts, "value": (float(price), f"5m_chart_{int(age_seconds)}s_old")})
                            return float(price), f"5m_chart_{int(age_seconds)}s_old"
            except Exception as e:
                logging.debug("Could not get chart price: %s", e)

            now = datetime.now(CT)
            is_market_closed = (
                now.weekday() == 5 or
                (now.weekday() == 6 and now.hour < 17) or
                (now.weekday() == 4 and now.hour >= 16)
            )
            if is_market_closed:
                try:
                    result = (
                        supabase
                        .table('tv_datafeed')
                        .select('c, ts')
                        .eq('symbol', symbol)
                        .in_('timeframe', _timeframe_filters(1))
                        .order('ts', desc=True)
                        .limit(1)
                        .execute()
                    )
                    if result.data:
                        record = result.data[0]
                        price = float(record.get('c'))
                        _PRICE_CACHE.update({"symbol": symbol, "ts": now_ts, "value": (price, "market_closed_last_known")})
                        return price, "market_closed_last_known"
                except Exception:
                    pass

        # Fallback to a lightweight REST query if the supabase client path fails
        fallback_price = _fetch_latest_price_from_supabase(symbol, timeframe="1m")
        if fallback_price is None:
            fallback_price = _fetch_latest_price_from_supabase(symbol, timeframe="5m")

        if fallback_price is not None:
            logging.info("Using fallback REST price for %s: $%s", symbol, fallback_price)
            _PRICE_CACHE.update({"symbol": symbol, "ts": now_ts, "value": (fallback_price, "rest_fallback")})
            return fallback_price, "rest_fallback"

        logging.warning("Could not determine current market price from any source")
        return None, None
    except Exception as e:
        logging.error(f"Error getting current market price: {e}")
        return None, None


def _fetch_latest_price_from_supabase(symbol: str, timeframe: str = "1m") -> Optional[float]:
    """Return the most recent close from the tv_datafeed Supabase table.

    This intentionally uses a minimal query to avoid pulling large payloads.
    """

    if not SUPABASE_URL or not SUPABASE_KEY:
        logging.debug("Supabase credentials missing; cannot fetch latest price")
        return None

    url = f"{SUPABASE_URL}/rest/v1/tv_datafeed"
    params = {
        "symbol": f"eq.{symbol}",
        "timeframe": f"eq.{timeframe}",
        "select": "c,ts",
        "order": "ts.desc",
        "limit": 1,
    }
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        resp = session.get(url, params=params, headers=headers, timeout=(3.05, 10))
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            logging.debug("Supabase tv_datafeed returned no rows for %s", symbol)
            return None

        latest = rows[0]
        price = latest.get("c") or latest.get("close")
        return float(price) if price is not None else None
    except Exception as exc:
        logging.warning("Failed to fetch latest price from Supabase: %s", exc)
        return None


def _compute_simple_position_context(
    positions: List[Dict], symbol: str, timeframe: str = "1m"
) -> Dict[str, Optional[float]]:
    """Compute a lightweight position context with a simple PnL calculation.

    This intentionally avoids complex risk logic; it only calculates size, average
    price, side, current price (from Supabase if available), and unrealized PnL.
    """

    if not positions:
        return {
            "has_position": False,
            "size": 0,
            "side": None,
            "average_price": None,
            "current_price": None,
            "unrealized_pnl": 0.0,
        }

    total_size = sum(p.get("size", 0) for p in positions)
    avg_price = (
        sum((p.get("averagePrice") or p.get("avgPrice") or 0) * p.get("size", 0) for p in positions)
        / total_size
        if total_size
        else 0
    )

    position_type = positions[0].get("type") if positions else None
    side = "LONG" if position_type == 1 else "SHORT" if position_type == 2 else None

    current_price = _fetch_latest_price_from_supabase(symbol or MES, timeframe)

    if current_price is None or avg_price is None:
        unrealized_pnl = 0.0
    elif side == "LONG":
        unrealized_pnl = (current_price - avg_price) * total_size
    elif side == "SHORT":
        unrealized_pnl = (avg_price - current_price) * total_size
    else:
        unrealized_pnl = 0.0

    return {
        "has_position": True,
        "size": total_size,
        "side": side,
        "average_price": avg_price,
        "current_price": current_price,
        "unrealized_pnl": unrealized_pnl,
    }


def ai_trade_decision(account, strat, sig, sym, size, alert, ai_url, positions=None, position_context=None):
    position_summary = _summarize_positions(positions or [])
    simple_position_context = position_context or _compute_simple_position_context(positions or [], sym)
    payload = {
        "account": account,
        "strategy": strat,
        "signal": sig,
        "symbol": sym,
        "size": size,
        "alert": alert,
        "positions": positions or [],
        "position_summary": position_summary,
        "position_context": simple_position_context,
    }
    try:
        resp = session.post(ai_url, json=payload, timeout=150)
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







