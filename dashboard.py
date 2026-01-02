import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytz
from dateutil import parser
from flask import Blueprint, jsonify, render_template, request

from api import get_contract, get_supabase_client
from config import load_config
from position_manager import PositionManager

config = load_config()
CT = config["CT"]
DENVER_TZ = pytz.timezone("America/Denver")
ACCOUNTS = config["ACCOUNTS"]
OVERRIDE_CONTRACT_ID = config.get("OVERRIDE_CONTRACT_ID")

logger = logging.getLogger(__name__)


dashboard_bp = Blueprint("dashboard", __name__, template_folder="templates")
POSITION_MANAGER = PositionManager(ACCOUNTS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_parse_timestamp(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = parser.parse(raw) if isinstance(raw, str) else raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _safe_json_loads(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw


def _extract_first_url(data: Any) -> Optional[str]:
    if not data:
        return None

    def _find_url(obj: Any) -> Optional[str]:
        if isinstance(obj, str):
            if obj.startswith("http://") or obj.startswith("https://"):
                return obj
            return None
        if isinstance(obj, dict):
            for val in obj.values():
                found = _find_url(val)
                if found:
                    return found
        if isinstance(obj, list):
            for val in obj:
                found = _find_url(val)
                if found:
                    return found
        return None

    return _find_url(data)


def _resolve_screenshot_url(record: Dict[str, Any]) -> Optional[str]:
    if record.get("screenshot_url"):
        return record.get("screenshot_url")

    urls_field = _safe_json_loads(record.get("urls"))
    url = _extract_first_url(urls_field)
    if url:
        return url

    decision_json = _safe_json_loads(record.get("decision_json"))
    url = _extract_first_url(decision_json)
    return url


def _resolve_reason(record: Dict[str, Any]) -> Optional[str]:
    reason = (record.get("reason") or "").strip()
    if reason:
        return reason
    decision_json = _safe_json_loads(record.get("decision_json"))
    if isinstance(decision_json, dict):
        reason = decision_json.get("reason")
        if reason:
            return str(reason)
    return None


def _format_ts_for_ui(raw: Any, tz: pytz.timezone) -> Optional[str]:
    dt = _safe_parse_timestamp(raw)
    if not dt:
        return None
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def _fetch_ai_trade_feed(
    limit: int = 200,
    account: str = "all",
    range_key: str = "7d",
    include_open: bool = True,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Pull ai_trade_feed rows with filtering and ordering."""

    try:
        supabase = get_supabase_client()
    except Exception as exc:
        logger.error("Supabase client unavailable: %s", exc)
        return [], str(exc)

    now_utc = datetime.now(timezone.utc)
    if range_key == "today":
        start_dt = datetime.now(DENVER_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    elif range_key == "30d":
        start_dt = now_utc.astimezone(DENVER_TZ) - timedelta(days=30)
    else:
        start_dt = now_utc.astimezone(DENVER_TZ) - timedelta(days=7)

    start_utc = start_dt.astimezone(timezone.utc)

    query = (
        supabase
        .table("ai_trade_feed")
        .select(
            "ai_decision_id, decision_time, entry_time, exit_time, account, symbol, signal, size, strategy, "
            "reason, screenshot_url, urls, total_pnl, fees_total, net_pnl, decision_json, updated_at"
        )
        .order("decision_time", desc=True)
        .order("ai_decision_id", desc=True)
        .gte("decision_time", start_utc.isoformat())
        .limit(limit)
    )

    if account != "all":
        query = query.eq("account", account)

    if not include_open:
        try:
            query = query.not_.is_("exit_time", None)
        except Exception:
            pass

    try:
        resp = query.execute()
        rows = resp.data or []
    except Exception as exc:
        logger.error("Error fetching ai_trade_feed: %s", exc)
        return [], str(exc)

    filtered_rows = []
    for r in rows:
        ts = _safe_parse_timestamp(r.get("decision_time") or r.get("updated_at"))
        if ts and ts < start_utc:
            continue
        if not include_open and not r.get("exit_time"):
            continue
        filtered_rows.append(r)

    parsed_rows: List[Dict[str, Any]] = []
    for r in filtered_rows:
        pnl = r.get("net_pnl") if r.get("net_pnl") is not None else r.get("total_pnl")
        parsed_rows.append({
            "ai_decision_id": r.get("ai_decision_id"),
            "decision_time": r.get("decision_time"),
            "entry_time": r.get("entry_time"),
            "exit_time": r.get("exit_time"),
            "account": r.get("account"),
            "symbol": r.get("symbol"),
            "signal": r.get("signal"),
            "size": r.get("size"),
            "strategy": r.get("strategy"),
            "reason_text": _resolve_reason(r),
            "screenshot_url": _resolve_screenshot_url(r),
            "pnl": pnl,
            "total_pnl": r.get("total_pnl"),
            "fees_total": r.get("fees_total"),
        })

    return parsed_rows, None


def _fetch_open_positions_snapshot(account: str = "all") -> Tuple[Dict[str, Any], Optional[str]]:
    cid = get_contract("MES") or OVERRIDE_CONTRACT_ID
    if not cid:
        return {"positions": [], "total_unrealized": 0}, "No contract configured"

    selected_accounts = ACCOUNTS.items() if account == "all" else [(account, ACCOUNTS.get(account))]
    positions: List[Dict[str, Any]] = []
    total_unrealized = 0.0

    for acct_name, acct_id in selected_accounts:
        if acct_id is None:
            continue
        try:
            state = POSITION_MANAGER.get_position_state_light(acct_id, cid)
        except Exception as exc:
            logger.error("Error fetching position for %s: %s", acct_name, exc)
            continue

        entry_time = _safe_parse_timestamp(state.get("creationTimestamp"))
        duration_minutes = state.get("duration_minutes")
        positions.append({
            "account": acct_name,
            "has_position": state.get("has_position"),
            "side": state.get("side"),
            "size": state.get("size"),
            "entry_price": state.get("entry_price"),
            "current_price": state.get("current_price"),
            "unrealized_pnl": state.get("unrealized_pnl"),
            "duration_minutes": duration_minutes,
            "entry_time": entry_time.isoformat() if entry_time else None,
        })
        try:
            total_unrealized += float(state.get("unrealized_pnl") or 0)
        except Exception:
            pass

    return {"positions": positions, "total_unrealized": total_unrealized}, None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _compute_metrics(rows: List[Dict[str, Any]], tz: pytz.timezone) -> Dict[str, Any]:
    now = datetime.now(tz)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_start = now - timedelta(days=7)

    def _pnl_value(row: Dict[str, Any]) -> float:
        try:
            return float(row.get("pnl") or 0)
        except Exception:
            return 0.0

    def _fees_value(row: Dict[str, Any]) -> float:
        try:
            return float(row.get("fees_total") or 0)
        except Exception:
            return 0.0

    def _in_range(row: Dict[str, Any], start: datetime) -> bool:
        ts = _safe_parse_timestamp(row.get("exit_time") or row.get("decision_time"))
        return bool(ts and ts.astimezone(tz) >= start)

    today_rows = [r for r in rows if r.get("exit_time") and _in_range(r, today_start)]
    seven_rows = [r for r in rows if r.get("exit_time") and _in_range(r, seven_start)]

    def _summarize(closed_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        pnls = [_pnl_value(r) for r in closed_rows]
        gross_pnls = [float(r.get("total_pnl") or r.get("pnl") or 0) for r in closed_rows]
        fees = [_fees_value(r) for r in closed_rows]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_wins = sum(p for p in gross_pnls if p > 0)
        gross_losses = sum(p for p in gross_pnls if p < 0)
        profit_factor = gross_wins / abs(gross_losses) if gross_losses != 0 else None

        return {
            "net_pnl": sum(pnls),
            "gross_pnl": sum(gross_pnls),
            "fees": sum(fees),
            "trade_count": len(closed_rows),
            "win_rate": (len(wins) / len(closed_rows) * 100) if closed_rows else 0,
            "avg_trade": (sum(pnls) / len(closed_rows)) if closed_rows else 0,
            "profit_factor": profit_factor,
        }

    def _streak(closed_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        sorted_rows = sorted(
            closed_rows,
            key=lambda r: _safe_parse_timestamp(r.get("exit_time") or r.get("decision_time")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        streak = 0
        last_result = None
        for row in sorted_rows:
            pnl = _pnl_value(row)
            result = "win" if pnl > 0 else "loss" if pnl < 0 else "flat"
            if last_result is None:
                streak = 1
                last_result = result
            elif result == last_result:
                streak += 1
            elif result == "flat":
                continue
            else:
                streak = 1
                last_result = result
        return {"streak": streak, "type": last_result}

    today_summary = _summarize(today_rows)
    seven_summary = _summarize(seven_rows)
    streak = _streak(today_rows)

    return {
        "today": today_summary,
        "seven_day": seven_summary,
        "streak": streak,
    }


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def _dashboard_payload(account: str, range_key: str, include_open: bool) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
    errors: Dict[str, str] = {}

    rows, err = _fetch_ai_trade_feed(account=account, range_key=range_key, include_open=include_open)
    if err:
        errors["ai_trade_feed"] = err
    payload["rows"] = rows

    metrics = _compute_metrics(rows, DENVER_TZ)
    payload["metrics"] = metrics

    open_positions, pos_err = _fetch_open_positions_snapshot(account)
    if pos_err:
        errors["open_positions"] = pos_err
    payload["open_positions"] = open_positions

    if errors:
        payload["errors"] = errors

    return payload


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@dashboard_bp.route("/dashboard")
def dashboard_page():
    accounts = list(ACCOUNTS.keys())
    return render_template("dashboard.html", accounts=accounts)


@dashboard_bp.route("/dashboard/data")
def dashboard_data():
    account = request.args.get("account", "all").lower()
    range_key = request.args.get("range", "7d")
    include_open_raw = request.args.get("include_open", "true").lower()
    include_open = include_open_raw != "false"

    if account != "all" and account not in ACCOUNTS:
        return jsonify({"error": "invalid account"}), 400

    payload = _dashboard_payload(account, range_key, include_open)
    return jsonify(payload)
