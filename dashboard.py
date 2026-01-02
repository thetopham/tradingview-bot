import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from dateutil import parser
from flask import Blueprint, jsonify, render_template, request

from api import get_contract, get_supabase_client
from config import load_config
from position_manager import PositionManager

config = load_config()
ACCOUNTS = config["ACCOUNTS"]
DEFAULT_ACCOUNT = config["DEFAULT_ACCOUNT"]
OVERRIDE_CONTRACT_ID = config.get("OVERRIDE_CONTRACT_ID")
MT_TZ = pytz.timezone("America/Denver")

logger = logging.getLogger(__name__)


dashboard_bp = Blueprint("dashboard", __name__, template_folder="templates")
_position_manager = PositionManager(ACCOUNTS)


def _safe_parse_json(value: Any) -> Optional[Any]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


def _looks_like_url(candidate: str) -> bool:
    if not isinstance(candidate, str):
        return False
    return candidate.startswith("http://") or candidate.startswith("https://")


def _extract_first_url(blob: Any) -> Optional[str]:
    if blob is None:
        return None
    if isinstance(blob, str):
        if _looks_like_url(blob):
            return blob
        parsed = _safe_parse_json(blob)
        if parsed is None:
            return None
        return _extract_first_url(parsed)
    if isinstance(blob, dict):
        for val in blob.values():
            url = _extract_first_url(val)
            if url:
                return url
    if isinstance(blob, list):
        for item in blob:
            url = _extract_first_url(item)
            if url:
                return url
    return None


def _resolve_screenshot(row: Dict[str, Any]) -> Optional[str]:
    if row.get("screenshot_url"):
        return row.get("screenshot_url")

    url = _extract_first_url(row.get("urls"))
    if url:
        return url

    decision_json = row.get("decision_json") or {}
    url = _extract_first_url(decision_json.get("urls") or decision_json.get("screenshot_url"))
    return url


def _resolve_reason(row: Dict[str, Any]) -> Optional[str]:
    reason = row.get("reason")
    if reason:
        return reason
    decision_json = row.get("decision_json") or {}
    return decision_json.get("reason")


def _parse_timestamp(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = parser.isoparse(ts)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=pytz.UTC)
    return dt


def _format_ts(ts: Optional[str], tz) -> Optional[str]:
    dt = _parse_timestamp(ts)
    if not dt:
        return None
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def _normalize_row(raw: Dict[str, Any], tz) -> Dict[str, Any]:
    pnl_value = raw.get("net_pnl") if raw.get("net_pnl") is not None else raw.get("total_pnl")
    screenshot_url = _resolve_screenshot(raw)
    reason = _resolve_reason(raw)

    normalized = {
        "decision_time_raw": raw.get("decision_time"),
        "entry_time_raw": raw.get("entry_time"),
        "exit_time_raw": raw.get("exit_time"),
        "decision_time": _format_ts(raw.get("decision_time"), tz),
        "entry_time": _format_ts(raw.get("entry_time"), tz),
        "exit_time": _format_ts(raw.get("exit_time"), tz),
        "account": raw.get("account"),
        "symbol": raw.get("symbol"),
        "signal": raw.get("signal"),
        "size": raw.get("size"),
        "strategy": raw.get("strategy"),
        "reason": reason,
        "screenshot_url": screenshot_url,
        "pnl": pnl_value,
        "fees_total": raw.get("fees_total"),
        "ai_decision_id": raw.get("ai_decision_id"),
    }
    return normalized


def _range_start(range_key: str, tz) -> datetime:
    now = datetime.now(tz)
    if range_key == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_key == "30d":
        return now - timedelta(days=30)
    return now - timedelta(days=7)


def _fetch_ai_trade_feed(
    limit: int = 200, account: str = "all", range_key: str = "7d", include_open: bool = True
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    try:
        supabase = get_supabase_client()
    except Exception as exc:
        logger.error("Supabase client unavailable: %s", exc)
        return [], f"Supabase client unavailable: {exc}"

    start_local = _range_start(range_key, MT_TZ)
    start_utc = start_local.astimezone(pytz.UTC)

    try:
        query = (
            supabase
            .table("ai_trade_feed")
            .select(
                "ai_decision_id, decision_time, entry_time, exit_time, account, symbol, signal, size, strategy, "
                "reason, screenshot_url, urls, total_pnl, fees_total, net_pnl, decision_json"
            )
            .order("decision_time", desc=True)
            .order("ai_decision_id", desc=True)
            .limit(limit)
        )

        if account and account != "all":
            query = query.eq("account", account)

        if range_key:
            query = query.gte("decision_time", start_utc.isoformat())

        if not include_open:
            query = query.not_.is_("exit_time", None)

        response = query.execute()
        rows = response.data or []
        return rows, None
    except Exception as exc:
        logger.error("Failed to fetch ai_trade_feed: %s", exc)
        return [], str(exc)


def _compute_profit_factor(gross_wins: float, gross_losses: float) -> Optional[float]:
    if gross_losses == 0:
        return None
    return gross_wins / abs(gross_losses)


def _compute_streak(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    sorted_rows = sorted(
        [r for r in rows if r.get("exit_time_raw") and r.get("pnl") is not None],
        key=lambda r: _parse_timestamp(r.get("exit_time_raw")) or datetime.min,
    )
    if not sorted_rows:
        return None

    last_direction = None
    streak = 0
    for row in sorted_rows:
        pnl = row.get("pnl")
        if pnl is None:
            continue
        direction = "win" if pnl > 0 else "loss" if pnl < 0 else "flat"
        if direction == "flat":
            last_direction = None
            streak = 0
            continue
        if last_direction is None or direction == last_direction:
            streak += 1
        else:
            streak = 1
        last_direction = direction
    if last_direction is None:
        return None
    return {"direction": last_direction, "count": streak}


def _compute_metrics(rows: List[Dict[str, Any]], range_key: str, tz) -> Dict[str, Any]:
    now = datetime.now(tz)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(pytz.UTC)
    start_7d = (now - timedelta(days=7)).astimezone(pytz.UTC)

    def _filter_closed(rows_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [r for r in rows_list if r.get("exit_time_raw")]

    def _rows_since(rows_list: List[Dict[str, Any]], start_dt: datetime) -> List[Dict[str, Any]]:
        filtered = []
        for r in rows_list:
            ts = _parse_timestamp(r.get("exit_time_raw") or r.get("decision_time_raw"))
            if ts and ts >= start_dt:
                filtered.append(r)
        return filtered

    closed_rows = _filter_closed(rows)
    today_rows = _rows_since(closed_rows, today_start)
    week_rows = _rows_since(closed_rows, start_7d)

    def _aggregate(rows_list: List[Dict[str, Any]]):
        gross_wins = sum(r.get("pnl", 0) for r in rows_list if (r.get("pnl") or 0) > 0)
        gross_losses = sum(r.get("pnl", 0) for r in rows_list if (r.get("pnl") or 0) < 0)
        total_fees = sum(r.get("fees_total") or 0 for r in rows_list)
        net = sum(r.get("pnl") or 0 for r in rows_list)
        trade_count = len(rows_list)
        wins = len([r for r in rows_list if (r.get("pnl") or 0) > 0])
        win_rate = (wins / trade_count * 100) if trade_count else None
        avg_trade = (net / trade_count) if trade_count else None
        profit_factor = _compute_profit_factor(gross_wins, gross_losses) if trade_count else None
        return {
            "net_pnl": net,
            "gross_pnl": gross_wins + gross_losses,
            "gross_wins": gross_wins,
            "gross_losses": gross_losses,
            "fees": total_fees,
            "trade_count": trade_count,
            "win_rate": win_rate,
            "avg_trade": avg_trade,
            "profit_factor": profit_factor,
        }

    metrics_today = _aggregate(today_rows)
    metrics_week = _aggregate(week_rows)
    streak = _compute_streak(today_rows)

    return {
        "range": range_key,
        "today": metrics_today,
        "seven_day": metrics_week,
        "streak": streak,
    }


def _fetch_open_positions_snapshot(account: str = "all") -> Tuple[List[Dict[str, Any]], Dict[str, Any], Optional[str]]:
    target_accounts = {account: ACCOUNTS[account]} if account != "all" and account in ACCOUNTS else ACCOUNTS
    cid = get_contract("MES") or OVERRIDE_CONTRACT_ID
    positions: List[Dict[str, Any]] = []
    total_unrealized = 0

    for acct_name, acct_id in target_accounts.items():
        try:
            state = _position_manager.get_position_state_light(acct_id, cid)
        except Exception as exc:
            logger.error("Failed to fetch position state for %s: %s", acct_name, exc)
            return positions, {"unrealized": total_unrealized}, str(exc)

        unrealized = state.get("unrealized_pnl") or 0
        total_unrealized += unrealized
        positions.append(
            {
                "account": acct_name,
                "has_position": state.get("has_position"),
                "side": state.get("side"),
                "size": state.get("size"),
                "entry_price": state.get("entry_price"),
                "current_price": state.get("current_price"),
                "unrealized_pnl": unrealized,
                "duration_minutes": state.get("duration_minutes"),
            }
        )

    totals = {"unrealized": total_unrealized}
    return positions, totals, None


def _dashboard_payload() -> Dict[str, Any]:
    account = request.args.get("account", "all").lower()
    range_key = request.args.get("range", "7d")
    include_open_str = request.args.get("include_open", "true").lower()
    include_open = include_open_str != "false"

    errors: Dict[str, str] = {}

    rows_raw, fetch_error = _fetch_ai_trade_feed(account=account, range_key=range_key, include_open=include_open)
    if fetch_error:
        errors["ai_trade_feed"] = fetch_error

    rows = [_normalize_row(r, MT_TZ) for r in rows_raw]
    metrics = _compute_metrics(rows, range_key, MT_TZ)

    open_positions, open_totals, pos_error = _fetch_open_positions_snapshot(account)
    if pos_error:
        errors["open_positions"] = pos_error

    payload = {
        "updated_at": datetime.now(pytz.UTC).isoformat(),
        "metrics": metrics,
        "open_positions": open_positions,
        "open_totals": open_totals,
        "rows": rows,
    }
    if errors:
        payload["errors"] = errors
    return payload


@dashboard_bp.route("/dashboard")
def dashboard():
    payload = _dashboard_payload()
    return render_template(
        "dashboard.html",
        payload=payload,
        accounts=list(ACCOUNTS.keys()),
        default_account=DEFAULT_ACCOUNT,
    )


@dashboard_bp.route("/dashboard/data")
def dashboard_data():
    payload = _dashboard_payload()
    return jsonify(payload)
