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
ACCOUNTS = config['ACCOUNTS']
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
        return ["1m", "1"]
    if window <= 5:
        return ["1m", "1", "5m", "5"]
    return ["1m", "1", "5m", "5", "15m", "15"]


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
                timeframe_filters = _timeframe_filters(1)
                timeframe_or_clause = ",".join(
                    f"timeframe.eq.\"{tf}\"" for tf in timeframe_filters
                )

                result = (
                    supabase
                    .table('tv_datafeed')
                    .select('c, ts')
                    .eq('symbol', symbol)
                    .or_(timeframe_or_clause)
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
                    timeframe_filters = _timeframe_filters(1)
                    timeframe_or_clause = ",".join(
                        f"timeframe.eq.\"{tf}\"" for tf in timeframe_filters
                    )

                    result = (
                        supabase
                        .table('tv_datafeed')
                        .select('c, ts')
                        .eq('symbol', symbol)
                        .or_(timeframe_or_clause)
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

    timeframe_variants = {str(timeframe)}
    if str(timeframe).endswith("m"):
        timeframe_variants.add(str(timeframe)[:-1])
    else:
        timeframe_variants.add(f"{timeframe}m")

    encoded_timeframes = ",".join(
        f"\"{tf}\"" for tf in sorted(timeframe_variants)
    )

    params = {
        "symbol": f"eq.{symbol}",
        "timeframe": f"in.({encoded_timeframes})",
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
    """Persist closed-trade results to Supabase with best-effort correlation.

    Key behaviors:
    - Normalizes timestamps (entry/exit) to tz-aware Chicago time for storage.
    - Queries ProjectX trades in a bounded window around the position lifecycle.
    - Retries briefly to avoid race conditions where fills/PnL arrive after the position-close event.
    - Uses trace_id (preferred) or ai_decision_id+entry_time window as an idempotency key to update instead of duplicating rows.
    """

    import json
    import time
    from datetime import datetime, timedelta, timezone
    import logging

    meta = meta or {}

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------
    def _account_slug_from_acct_id() -> str:
        try:
            for name, _id in ACCOUNTS.items():
                if _id == acct_id:
                    return str(name)
        except Exception:
            pass
        return str(acct_id)

    def _normalize_entry_time(value) -> datetime:
        """Normalize entry_time to a tz-aware Chicago datetime."""
        try:
            if isinstance(value, (float, int)):
                return datetime.fromtimestamp(value, CT)

            if isinstance(value, str):
                return parser.isoparse(value).astimezone(CT)

            if getattr(value, "tzinfo", None) is None:
                return CT.localize(value)

            return value.astimezone(CT)
        except Exception as exc:
            logging.error(
                "[log_trade_results_to_supabase] entry_time conversion error: %r (%s): %s",
                value,
                type(value),
                exc,
            )
            return datetime.now(CT)

    def _parse_trade_ts(trade: dict) -> datetime | None:
        raw = (
            trade.get("creationTimestamp")
            or trade.get("timestamp")
            or trade.get("time")
            or trade.get("ts")
        )
        if not raw:
            return None
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), timezone.utc)
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        if isinstance(raw, str):
            try:
                dt = parser.isoparse(raw)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                return None
        return None

    def _extract_order_ids(value) -> set[str]:
        ids: set[str] = set()
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if item is not None:
                    ids.add(str(item))
        elif value is not None:
            ids.add(str(value))
        return ids

    # ---------------------------------------------------------------------
    # Normalize context
    # ---------------------------------------------------------------------
    entry_dt = _normalize_entry_time(entry_time)
    exit_guess = datetime.now(CT)

    # Query window (buffered to handle slight clock skew + partial fills)
    start_dt = entry_dt - timedelta(minutes=5)
    end_dt = exit_guess + timedelta(minutes=2)
    start_utc = start_dt.astimezone(timezone.utc)
    end_utc = end_dt.astimezone(timezone.utc)

    account_slug = str(meta.get("account") or _account_slug_from_acct_id())
    symbol_value = str(meta.get("symbol") or cid or "")
    trace_id = meta.get("trace_id")

    order_ids = _extract_order_ids(meta.get("order_id"))
    
    def _extract_trade_fees(trade: dict) -> float:
        """Return total fees/commissions for a trade record (absolute)."""
        if not isinstance(trade, dict):
            return 0.0

        def _to_float(value) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        # ProjectX commonly uses `fees`
        preferred_keys = (
            "fees",
            "commission",
            "commissionAndFees",
            "totalFees",
            "feesTotal",
            "brokerageFeesTotal",
        )
        for key in preferred_keys:
            if key in trade and trade.get(key) is not None:
                v = _to_float(trade.get(key))
                if v:
                    return abs(v)

        fee_sum = 0.0
        for k, v in trade.items():
            if isinstance(k, str) and ("fee" in k.lower() or "commission" in k.lower()):
                fee_sum += abs(_to_float(v))
        return fee_sum

    def _signed_qty(trade: dict) -> float:
        """Buy=+size, Sell=-size (ProjectX side: 0=BUY, 1=SELL)."""
        try:
            qty = float(trade.get("size") or 0)
        except Exception:
            return 0.0
        if qty <= 0:
            return 0.0
        side = trade.get("side")
        if side == 0:
            return qty
        if side == 1:
            return -qty
        return 0.0

    def _slice_round_trip(trades: list[dict], entry_order_ids: set[str], entry_dt_ct: datetime) -> list[dict]:
        """Slice trades to *this* position: start at entry orderId, stop when position returns to flat."""
        if not trades:
            return []

        start_idx = 0

        # Prefer anchoring to the entry order_id(s)
        if entry_order_ids:
            for i, t in enumerate(trades):
                if str(t.get("orderId")) in entry_order_ids:
                    start_idx = i
                    break
            else:
                # Fallback: first trade at/after entry time (30s buffer)
                entry_utc = entry_dt_ct.astimezone(timezone.utc)
                for i, t in enumerate(trades):
                    ts = _parse_trade_ts(t)
                    if ts and ts >= (entry_utc - timedelta(seconds=30)):
                        start_idx = i
                        break

        sliced: list[dict] = []
        pos = 0.0

        for t in trades[start_idx:]:
            delta = _signed_qty(t)
            if delta == 0:
                continue
            pos += delta
            sliced.append(t)

            # stop when flat AND we've seen at least one pnl-bearing trade
            if abs(pos) < 1e-9 and any(x.get("profitAndLoss") is not None for x in sliced):
                break

        return sliced or trades

    # ---------------------------------------------------------------------
    # Trade search with retries to avoid close-event race conditions
    # ---------------------------------------------------------------------
    def _fetch_relevant_trades():
        resp = post(
            "/api/Trade/search",
            {
                "accountId": acct_id,
                "startTimestamp": start_utc.isoformat(),
            },
        )
        trades = resp.get("trades", []) or []

        relevant = []
        for t in trades:
            if not isinstance(t, dict):
                continue
            if t.get("voided", False):
                continue
            if (t.get("size") or 0) <= 0:
                continue
            if t.get("contractId") != cid:
                continue

            t_ts = _parse_trade_ts(t)
            if t_ts and t_ts.astimezone(timezone.utc) > end_utc:
                continue

            relevant.append(t)

        # stable sort by timestamp if available
        relevant.sort(key=lambda x: (_parse_trade_ts(x) or datetime.min.replace(tzinfo=timezone.utc)))

        # NEW: slice to this specific entry->flat lifecycle (prevents mixing other round-trips)
        session_trades = _slice_round_trip(relevant, order_ids, entry_dt)
        return session_trades, trades


    sleeps = [0.5, 1, 2, 3, 5, 8]
    relevant_trades: list[dict] = []
    all_trades: list[dict] = []

    for attempt in range(len(sleeps) + 1):
        try:
            relevant_trades, all_trades = _fetch_relevant_trades()
        except Exception as exc:
            logging.error("[log_trade_results_to_supabase] Trade search failed: %s", exc)
            relevant_trades, all_trades = [], []

        has_pnl = any(t.get("profitAndLoss") is not None for t in relevant_trades)

        if relevant_trades and has_pnl:
            break

        if attempt < len(sleeps):
            if relevant_trades and not has_pnl:
                logging.info(
                    "[log_trade_results_to_supabase] Trades found but PnL not ready yet (attempt %s/%s). Retrying...",
                    attempt + 1,
                    len(sleeps) + 1,
                )
            else:
                logging.info(
                    "[log_trade_results_to_supabase] No relevant trades yet (attempt %s/%s). Retrying...",
                    attempt + 1,
                    len(sleeps) + 1,
                )
            time.sleep(sleeps[attempt])

    if not relevant_trades:
        logging.warning(
            "[log_trade_results_to_supabase] No relevant trades found for acct=%s cid=%s (window %s → %s). Skipping Supabase log.",
            acct_id,
            cid,
            start_utc.isoformat(),
            end_utc.isoformat(),
        )
        try:
            with open("/tmp/trade_results_missing.jsonl", "a") as f:
                f.write(
                    json.dumps(
                        {
                            "acct_id": acct_id,
                            "cid": cid,
                            "entry_time": entry_dt.isoformat(),
                            "exit_time_guess": exit_guess.isoformat(),
                            "ai_decision_id": ai_decision_id,
                            "meta": meta,
                            "all_trades": all_trades,
                        }
                    )
                    + "\n"
                )
        except Exception as e2:
            logging.error("[log_trade_results_to_supabase] Failed to write missing-trade log: %s", e2)
        return

    # Choose exit_time as the last observed trade timestamp (fallback to now)
    trade_times = [ts for ts in (_parse_trade_ts(t) for t in relevant_trades) if ts is not None]
    exit_dt = max(trade_times).astimezone(CT) if trade_times else exit_guess

    # Compute gross PnL (sum only the trade records that report profitAndLoss)
    pnl_values: list[float] = []
    for t in relevant_trades:
        pnl = t.get("profitAndLoss")
        if pnl is None:
            continue
        try:
            pnl_values.append(float(pnl))
        except Exception:
            continue

    gross_pnl = float(sum(pnl_values)) if pnl_values else 0.0

    # Fees/commissions: include entry + exit fills
    fees_total = float(sum(_extract_trade_fees(t) for t in relevant_trades)) if relevant_trades else 0.0

    # Net = gross - fees
    net_pnl = gross_pnl - fees_total


    trade_ids = [t.get("id") for t in relevant_trades if t.get("id") is not None]
    duration_sec = int(max((exit_dt - entry_dt).total_seconds(), 0))

    # Provide a helpful note if the contract/time-window match did not include the entry order id.
    if order_ids:
        matched_orders = sum(1 for t in relevant_trades if str(t.get("orderId")) in order_ids)
        if matched_orders == 0:
            logging.warning(
                "[log_trade_results_to_supabase] Trade window matched contract but not entry order_id(s)=%s; continuing anyway.",
                sorted(order_ids),
            )

    # ---------------------------------------------------------------------
    # AI decision id normalization + recovery
    # ---------------------------------------------------------------------
    def _recover_ai_id_from_ai_log(entry_dt_ct: datetime) -> int | None:
        """Attempt to recover ai_decision_id from ai_trading_log near entry_time."""
        try:
            supabase = get_supabase_client()
        except Exception as e:
            logging.warning(
                "[log_trade_results_to_supabase] Unable to init Supabase client for ai_trading_log recovery: %s",
                e,
            )
            return None

        try:
            start = (entry_dt_ct - timedelta(minutes=10)).astimezone(timezone.utc).isoformat()
            end = (entry_dt_ct + timedelta(minutes=10)).astimezone(timezone.utc).isoformat()
            res = (
                supabase.table("ai_trading_log")
                .select("ai_decision_id,timestamp")
                .eq("account", account_slug)
                .eq("symbol", symbol_value)
                .gte("timestamp", start)
                .lte("timestamp", end)
                .order("timestamp", desc=True)
                .limit(1)
                .execute()
            )
            for row in res.data or []:
                candidate = row.get("ai_decision_id")
                if candidate is None:
                    continue
                try:
                    return int(candidate)
                except Exception:
                    continue
        except Exception as e:
            logging.error("[log_trade_results_to_supabase] Failed ai_trading_log recovery: %s", e)

        return None

    ai_decision_id_out = None
    ai_decision_note = ""

    if ai_decision_id is not None:
        try:
            ai_decision_id_out = int(ai_decision_id)
        except (ValueError, TypeError):
            ai_decision_note = f"ai_decision={ai_decision_id}"

            # Try a lenient parse for strings like "123 something"
            try:
                ai_decision_id_out = int(str(ai_decision_id).strip().split()[0])
            except Exception:
                ai_decision_id_out = None

    if ai_decision_id_out is None:
        recovered = _recover_ai_id_from_ai_log(entry_dt)
        if recovered is not None:
            ai_decision_id_out = recovered
            ai_decision_note = (ai_decision_note + "|recovered_from_ai_log").strip("|")

    # ---------------------------------------------------------------------
    # Build payload
    # ---------------------------------------------------------------------
    base_comment = str(meta.get("comment") or "")
    comment_parts = [
        part
        for part in [
            base_comment,
            f"trace_id={trace_id}" if trace_id else None,
            ai_decision_note or None,
            "pnl_missing" if not pnl_values else None,
        ]
        if part
    ]
    comment = " | ".join(comment_parts)

    payload = {
        "strategy": str(meta.get("strategy") or ""),
        "signal": str(meta.get("signal") or ""),
        "symbol": symbol_value,
        "account": account_slug,
        "size": int(meta.get("size") or 0),
        "ai_decision_id": ai_decision_id_out,
        "entry_time": entry_dt.isoformat(),
        "exit_time": exit_dt.isoformat(),
        "duration_sec": duration_sec,
        "alert": str(meta.get("alert") or ""),
        "total_pnl": gross_pnl,
        "fees_total": fees_total,
        "net_pnl": net_pnl,
        "raw_trades": relevant_trades if relevant_trades else [],
        "order_id": json.dumps(sorted(order_ids)) if order_ids else str(meta.get("order_id") or ""),
        "comment": comment,
        "trade_ids": trade_ids if trade_ids else [],
        "trace_id": trace_id,
        "session_id": meta.get("session_id"),
    }

    # ---------------------------------------------------------------------
    # Idempotency guard: update existing row instead of inserting duplicates
    # ---------------------------------------------------------------------
    try:
        supabase = get_supabase_client()
        existing = None

        if trace_id:
            res = (
                supabase.table("trade_results")
                .select("id,ai_decision_id,trace_id,total_pnl,comment")
                .eq("trace_id", trace_id)
                .limit(1)
                .execute()
            )
            existing = (res.data or [None])[0]

        if not existing and ai_decision_id_out is not None:
            start = (entry_dt - timedelta(minutes=10)).isoformat()
            end = (entry_dt + timedelta(minutes=10)).isoformat()
            res = (
                supabase.table("trade_results")
                .select("id,ai_decision_id,entry_time,total_pnl,comment,trace_id")
                .eq("ai_decision_id", ai_decision_id_out)
                .gte("entry_time", start)
                .lte("entry_time", end)
                .order("entry_time", desc=True)
                .limit(1)
                .execute()
            )
            existing = (res.data or [None])[0]

        if existing:
            updates = {
                "exit_time": payload["exit_time"],
                "duration_sec": payload["duration_sec"],
                "total_pnl": payload["total_pnl"],
                "fees_total": payload.get("fees_total"),
                "net_pnl": payload.get("net_pnl"),
                "raw_trades": payload["raw_trades"],
                "trade_ids": payload["trade_ids"],
                "order_id": payload["order_id"],
                "comment": payload["comment"],
            }
            if payload.get("ai_decision_id") is not None and existing.get("ai_decision_id") in (None, ""):
                updates["ai_decision_id"] = payload["ai_decision_id"]
            if trace_id and not existing.get("trace_id"):
                updates["trace_id"] = trace_id

            supabase.table("trade_results").update(updates).eq("id", existing["id"]).execute()
            logging.info(
                "[log_trade_results_to_supabase] Updated existing trade result id=%s (trace_id=%s ai_decision_id=%s)",
                existing["id"],
                trace_id,
                ai_decision_id_out,
            )
            return
    except Exception as e:
        logging.warning("[log_trade_results_to_supabase] Idempotency update failed: %s", e)

    # Insert to Supabase
    url = f"{SUPABASE_URL}/rest/v1/trade_results"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    try:
        r = session.post(url, json=payload, headers=headers, timeout=(3.05, 10))
        if r.status_code == 201:
            logging.info(
                "[log_trade_results_to_supabase] Uploaded trade result for acct=%s, cid=%s, PnL=%s, ai_decision_id=%s, trace_id=%s",
                acct_id,
                cid,
                total_pnl,
                ai_decision_id_out,
                trace_id,
            )
        else:
            logging.warning(
                "[log_trade_results_to_supabase] Supabase returned non-201: status=%s, text=%s",
                r.status_code,
                r.text,
            )
        r.raise_for_status()
    except Exception as e:
        logging.error("[log_trade_results_to_supabase] Supabase upload failed: %s", e)
        try:
            with open("/tmp/trade_results_fallback.jsonl", "a") as f:
                f.write(json.dumps(payload) + "\n")
            logging.info("[log_trade_results_to_supabase] Trade result written to local fallback log.")
        except Exception as e2:
            logging.error("[log_trade_results_to_supabase] Failed to write trade result to local log: %s", e2)
