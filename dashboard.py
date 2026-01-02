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
ACCOUNTS = config["ACCOUNTS"]

DASHBOARD_TZ = pytz.timezone("America/Denver")
logger = logging.getLogger(__name__)

position_manager = PositionManager(ACCOUNTS)

dashboard_bp = Blueprint("dashboard", __name__)


# ─── Helpers ───────────────────────────────────────────

def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = parser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _format_dt(value: Optional[str], tz: pytz.timezone = DASHBOARD_TZ) -> str:
    dt = _parse_dt(value)
    if not dt:
        return ""
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def _looks_like_url(text: str) -> bool:
    return text.startswith("http://") or text.startswith("https://")


def _extract_first_url(value: Any) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, str):
        text = value.strip()
        if _looks_like_url(text):
            return text
        try:
            parsed = json.loads(text)
            return _extract_first_url(parsed)
        except Exception:
            return None

    if isinstance(value, dict):
        for v in value.values():
            url = _extract_first_url(v)
            if url:
                return url
        return None

    if isinstance(value, (list, tuple, set)):
        for item in value:
            url = _extract_first_url(item)
            if url:
                return url
        return None

    return None


def _resolve_screenshot(row: Dict) -> Optional[str]:
    if row.get("screenshot_url") and _looks_like_url(str(row.get("screenshot_url"))):
        return row.get("screenshot_url")

    url_candidates = _extract_first_url(row.get("urls"))
    if url_candidates:
        return url_candidates

    decision_json = row.get("decision_json") or {}
    url_from_decision = _extract_first_url(decision_json.get("urls") or decision_json.get("screenshot_url"))
    if url_from_decision:
        return url_from_decision

    return None


def _resolve_reason(row: Dict) -> Optional[str]:
    reason = row.get("reason")
    if reason:
        return reason
    decision_json = row.get("decision_json") or {}
    return decision_json.get("reason")


def _resolve_pnl(row: Dict) -> Optional[float]:
    pnl = row.get("net_pnl")
    if pnl is not None:
        try:
            return float(pnl)
        except Exception:
            return None
    pnl = row.get("total_pnl")
    if pnl is None:
        return None
    try:
        return float(pnl)
    except Exception:
        return None


def _range_start(range_label: str, tz: pytz.timezone = DASHBOARD_TZ) -> datetime:
    now = datetime.now(tz)
    if range_label == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_label == "30d":
        return now - timedelta(days=30)
    return now - timedelta(days=7)


# ─── Supabase Fetch ────────────────────────────────────

def _fetch_ai_trade_feed(
    *, limit: int = 200, account: str = "all", range_label: str = "7d", include_open: bool = True
) -> Tuple[List[Dict], Optional[str]]:
    client = None
    try:
        client = get_supabase_client()
    except Exception as exc:
        logger.error("Supabase init error: %s", exc)
        return [], str(exc)

    start_ts_local = _range_start(range_label)
    start_ts_utc = start_ts_local.astimezone(timezone.utc).isoformat()

    try:
        query = (
            client
            .table("ai_trade_feed")
            .select(
                "ai_decision_id, decision_time, entry_time, exit_time, account, symbol, signal, size, "
                "strategy, reason, screenshot_url, urls, total_pnl, fees_total, net_pnl, decision_json, updated_at"
            )
            .order("decision_time", desc=True)
            .order("ai_decision_id", desc=True)
            .limit(limit)
        )

        if account and account != "all":
            query = query.eq("account", account)

        if start_ts_utc:
            query = query.gte("decision_time", start_ts_utc)

        response = query.execute()
        data = response.data or []
    except Exception as exc:
        logger.error("Supabase query failed: %s", exc)
        return [], str(exc)

    rows: List[Dict] = []
    for row in data:
        if not include_open and row.get("exit_time") in (None, ""):
            continue

        decision_time = row.get("decision_time") or row.get("updated_at")
        decision_dt = _parse_dt(decision_time)
        if decision_dt and decision_dt.astimezone(DASHBOARD_TZ) < start_ts_local:
            continue

        entry_time = row.get("entry_time")
        exit_time = row.get("exit_time")

        rows.append(
            {
                "decision_time": decision_time,
                "decision_time_display": _format_dt(decision_time),
                "entry_time": entry_time,
                "entry_time_display": _format_dt(entry_time),
                "exit_time": exit_time,
                "exit_time_display": _format_dt(exit_time),
                "account": row.get("account"),
                "symbol": row.get("symbol"),
                "signal": row.get("signal"),
                "size": row.get("size"),
                "strategy": row.get("strategy"),
                "reason": _resolve_reason(row),
                "screenshot": _resolve_screenshot(row),
                "ai_decision_id": row.get("ai_decision_id"),
                "total_pnl": row.get("total_pnl"),
                "net_pnl": row.get("net_pnl"),
                "fees_total": row.get("fees_total"),
                "pnl": _resolve_pnl(row),
            }
        )

    return rows, None


# ─── Metrics ───────────────────────────────────────────

def _compute_metrics(rows: List[Dict], range_label: str, tz: pytz.timezone = DASHBOARD_TZ) -> Dict:
    metrics: Dict[str, Any] = {}

    now = datetime.now(tz)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_7d = now - timedelta(days=7)

    closed_rows = [r for r in rows if r.get("exit_time")]
    today_closed = [
        r for r in closed_rows if (_parse_dt(r.get("decision_time")) or _parse_dt(r.get("exit_time")))
        and (_parse_dt(r.get("decision_time")) or _parse_dt(r.get("exit_time"))).astimezone(tz) >= start_today
    ]

    def _sum_field(items: List[Dict], key: str) -> float:
        total = 0.0
        for i in items:
            val = i.get(key)
            if val is None:
                continue
            try:
                total += float(val)
            except Exception:
                continue
        return total

    def _sum_pnl(items: List[Dict]) -> float:
        total = 0.0
        for i in items:
            pnl_val = _resolve_pnl(i)
            if pnl_val is None:
                continue
            total += pnl_val
        return total

    today_net = _sum_pnl(today_closed)
    today_gross = _sum_field(today_closed, "total_pnl")
    today_fees = _sum_field(today_closed, "fees_total")
    today_count = len(today_closed)
    today_wins = len([r for r in today_closed if (_resolve_pnl(r) or 0) > 0])
    today_losses = len([r for r in today_closed if (_resolve_pnl(r) or 0) < 0])
    today_win_pct = (today_wins / today_count * 100) if today_count else 0
    today_avg = (today_net / today_count) if today_count else 0

    total_wins_amt = _sum_pnl([r for r in today_closed if (_resolve_pnl(r) or 0) > 0])
    total_losses_amt = _sum_pnl([r for r in today_closed if (_resolve_pnl(r) or 0) < 0])
    profit_factor = total_wins_amt / abs(total_losses_amt) if total_losses_amt else None

    # Win/loss streak for today
    streak = {"direction": None, "count": 0}
    if today_closed:
        sorted_today = sorted(
            today_closed,
            key=lambda r: _parse_dt(r.get("exit_time")) or _parse_dt(r.get("decision_time")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        current = None
        count = 0
        for r in sorted_today:
            pnl_val = _resolve_pnl(r) or 0
            outcome = "win" if pnl_val > 0 else "loss" if pnl_val < 0 else "flat"
            if outcome == "flat":
                continue
            if current is None or outcome == current:
                count += 1
                current = outcome
            else:
                count = 1
                current = outcome
        if current:
            streak = {"direction": current, "count": count}

    seven_day_closed = [
        r for r in closed_rows
        if (_parse_dt(r.get("decision_time")) or _parse_dt(r.get("exit_time")))
        and (_parse_dt(r.get("decision_time")) or _parse_dt(r.get("exit_time"))).astimezone(tz) >= start_7d
    ]
    seven_day_net = _sum_pnl(seven_day_closed)
    seven_day_wins = len([r for r in seven_day_closed if (_resolve_pnl(r) or 0) > 0])
    seven_day_win_pct = (seven_day_wins / len(seven_day_closed) * 100) if seven_day_closed else 0

    metrics.update(
        {
            "today_net": today_net,
            "today_gross": today_gross,
            "today_fees": today_fees,
            "today_trade_count": today_count,
            "today_win_pct": today_win_pct,
            "today_avg": today_avg,
            "profit_factor": profit_factor,
            "streak": streak,
            "seven_day_net": seven_day_net,
            "seven_day_win_pct": seven_day_win_pct,
        }
    )

    return metrics


# ─── Open Position Snapshot ────────────────────────────

def _fetch_open_positions_snapshot(account: str = "all") -> Tuple[List[Dict], Dict[str, Any]]:
    accounts = [account] if account != "all" and account in ACCOUNTS else list(ACCOUNTS.keys())
    cid = get_contract("MES")
    open_positions: List[Dict[str, Any]] = []
    totals = {"unrealized_pnl": 0.0}

    for acct in accounts:
        acct_id = ACCOUNTS.get(acct)
        if acct_id is None:
            continue

        try:
            snapshot = position_manager.get_position_state_light(acct_id, cid)
        except Exception as exc:
            logger.error("Position snapshot failed for %s: %s", acct, exc)
            continue

        open_positions.append(
            {
                "account": acct,
                "has_position": snapshot.get("has_position"),
                "side": snapshot.get("side"),
                "size": snapshot.get("size"),
                "entry_price": snapshot.get("entry_price"),
                "current_price": snapshot.get("current_price"),
                "unrealized_pnl": snapshot.get("unrealized_pnl"),
                "duration_minutes": snapshot.get("duration_minutes"),
            }
        )

        try:
            totals["unrealized_pnl"] += float(snapshot.get("unrealized_pnl") or 0)
        except Exception:
            pass

    return open_positions, totals


# ─── Payload Builder ───────────────────────────────────

def _dashboard_payload(account: str, range_label: str, include_open: bool) -> Dict:
    rows, fetch_error = _fetch_ai_trade_feed(account=account, range_label=range_label, include_open=include_open)
    metrics = _compute_metrics(rows, range_label, DASHBOARD_TZ)
    open_positions, open_totals = _fetch_open_positions_snapshot(account)

    payload: Dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "open_positions": open_positions,
        "open_totals": open_totals,
        "rows": rows,
    }

    errors = {}
    if fetch_error:
        errors["ai_trade_feed"] = fetch_error
    if errors:
        payload["errors"] = errors

    return payload


# ─── Routes ────────────────────────────────────────────

@dashboard_bp.route("/dashboard")
def dashboard_page():
    account = request.args.get("account", "all")
    range_label = request.args.get("range", "7d")
    include_open_raw = request.args.get("include_open", "true")
    include_open = include_open_raw in (True, "true", "1", "yes", "on")

    payload = _dashboard_payload(account=account, range_label=range_label, include_open=include_open)
    return render_template(
        "dashboard.html",
        payload=payload,
        accounts=list(ACCOUNTS.keys()),
        default_account=account,
        default_range=range_label,
        default_include_open=include_open,
    )


@dashboard_bp.route("/dashboard/data")
def dashboard_data():
    account = request.args.get("account", "all")
    range_label = request.args.get("range", "7d")
    include_open_raw = request.args.get("include_open", "true")
    include_open = include_open_raw in (True, "true", "1", "yes", "on")

    payload = _dashboard_payload(account=account, range_label=range_label, include_open=include_open)
    return jsonify(payload)

