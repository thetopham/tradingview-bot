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
DEFAULT_ACCOUNT = config["DEFAULT_ACCOUNT"]
MOUNTAIN_TZ = pytz.timezone("America/Denver")

logger = logging.getLogger(__name__)


dashboard_bp = Blueprint("dashboard", __name__, template_folder="templates", static_folder="static")
position_manager = PositionManager(ACCOUNTS)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_json_loads(val: Any) -> Optional[Any]:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return None
    return None


def _looks_like_url(candidate: Any) -> bool:
    if not isinstance(candidate, str):
        return False
    return candidate.startswith("http://") or candidate.startswith("https://")


def _extract_first_url(candidate: Any) -> Optional[str]:
    if candidate is None:
        return None
    if isinstance(candidate, str):
        return candidate if _looks_like_url(candidate) else None
    if isinstance(candidate, dict):
        for _, value in candidate.items():
            found = _extract_first_url(value)
            if found:
                return found
    if isinstance(candidate, list):
        for value in candidate:
            found = _extract_first_url(value)
            if found:
                return found
    return None


def _resolve_reason(record: Dict[str, Any]) -> Optional[str]:
    reason = record.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()

    decision_json = record.get("decision_json") or {}
    if isinstance(decision_json, str):
        decision_json = _safe_json_loads(decision_json) or {}

    if isinstance(decision_json, dict):
        reason = decision_json.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    return None


def _resolve_screenshot(record: Dict[str, Any]) -> Optional[str]:
    screenshot_url = record.get("screenshot_url") or None
    if isinstance(screenshot_url, str) and screenshot_url.strip():
        screenshot_url = screenshot_url.strip()
        if _looks_like_url(screenshot_url):
            return screenshot_url

    urls = record.get("urls")
    parsed_urls = _safe_json_loads(urls)
    if parsed_urls is not None:
        found = _extract_first_url(parsed_urls)
        if found:
            return found
    if isinstance(urls, str) and _looks_like_url(urls.strip()):
        return urls.strip()

    decision_json = record.get("decision_json") or {}
    if isinstance(decision_json, str):
        decision_json = _safe_json_loads(decision_json) or {}
    if isinstance(decision_json, dict):
        found = _extract_first_url(decision_json.get("urls"))
        if found:
            return found
        found = decision_json.get("screenshot_url") or decision_json.get("screenshot")
        if _looks_like_url(found):
            return found
    return None


def _coerce_dt(raw_val: Any) -> Optional[datetime]:
    if raw_val is None:
        return None
    if isinstance(raw_val, datetime):
        if raw_val.tzinfo is None:
            return raw_val.replace(tzinfo=timezone.utc)
        return raw_val
    try:
        dt = parser.parse(str(raw_val))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _range_start_iso(range_key: str) -> Optional[str]:
    now_mt = datetime.now(MOUNTAIN_TZ)
    if range_key == "today":
        start_mt = now_mt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif range_key == "30d":
        start_mt = now_mt - timedelta(days=30)
    else:
        start_mt = now_mt - timedelta(days=7)
    return start_mt.astimezone(timezone.utc).isoformat()


def _resolve_pnl(record: Dict[str, Any]) -> Optional[float]:
    pnl = record.get("net_pnl")
    if pnl is None:
        pnl = record.get("total_pnl")
    try:
        return float(pnl) if pnl is not None else None
    except Exception:
        return None


def _compute_profit_factor(gross_wins: float, gross_losses: float) -> Optional[float]:
    if gross_losses < 0:
        gross_losses = abs(gross_losses)
    if gross_losses == 0:
        return None
    return gross_wins / gross_losses


# ─── Supabase Fetch ───────────────────────────────────────────────────────────

def _fetch_ai_trade_feed(
    *, limit: int = 200, account: str = "all", range_key: str = "7d", include_open: bool = True
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, str]]]:
    errors: Optional[Dict[str, str]] = None
    try:
        sb = get_supabase_client()
        columns = (
            "ai_decision_id,decision_time,entry_time,exit_time,account,symbol,signal,size,"
            "strategy,reason,screenshot_url,urls,total_pnl,fees_total,net_pnl,decision_json,updated_at"
        )
        query = sb.table("ai_trade_feed").select(columns)
        if account != "all":
            query = query.eq("account", account)

        start_iso = _range_start_iso(range_key)
        if start_iso:
            query = query.gte("decision_time", start_iso)

        query = query.order("decision_time", desc=True).order("ai_decision_id", desc=True).limit(limit)
        resp = query.execute()
        data = resp.data or []
    except Exception as exc:  # noqa: PERF203
        logger.exception("Error fetching ai_trade_feed: %s", exc)
        return [], {"fetch": str(exc)}

    rows: List[Dict[str, Any]] = []
    for record in data:
        exit_time = record.get("exit_time")
        if not include_open and exit_time is None:
            continue

        decision_dt = _coerce_dt(record.get("decision_time")) or _coerce_dt(record.get("updated_at"))
        entry_dt = _coerce_dt(record.get("entry_time"))
        exit_dt = _coerce_dt(record.get("exit_time"))

        resolved = {
            "ai_decision_id": record.get("ai_decision_id"),
            "decision_time": decision_dt.isoformat() if decision_dt else None,
            "entry_time": entry_dt.isoformat() if entry_dt else None,
            "exit_time": exit_dt.isoformat() if exit_dt else None,
            "account": record.get("account"),
            "symbol": record.get("symbol"),
            "signal": record.get("signal"),
            "size": record.get("size"),
            "strategy": record.get("strategy"),
            "pnl": _resolve_pnl(record),
            "net_pnl": record.get("net_pnl"),
            "total_pnl": record.get("total_pnl"),
            "fees_total": record.get("fees_total"),
            "reason": _resolve_reason(record),
            "screenshot": _resolve_screenshot(record),
        }
        rows.append(resolved)

    rows.sort(
        key=lambda r: (
            _coerce_dt(r.get("decision_time")) or datetime.min.replace(tzinfo=timezone.utc),
            r.get("ai_decision_id") or 0,
        ),
        reverse=True,
    )
    return rows, errors


# ─── Metrics ──────────────────────────────────────────────────────────────────

def _filter_closed_trades(rows: List[Dict[str, Any]], start_dt_tz: datetime, tz) -> List[Tuple[datetime, Dict[str, Any]]]:
    start_utc = start_dt_tz.astimezone(timezone.utc)
    closed: List[Tuple[datetime, Dict[str, Any]]] = []
    for row in rows:
        exit_dt = _coerce_dt(row.get("exit_time"))
        if exit_dt and exit_dt >= start_utc:
            closed.append((exit_dt.astimezone(tz), row))
    return closed


def _streak_for_trades(closed: List[Tuple[datetime, Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    if not closed:
        return None
    streak_type = None
    streak_count = 0
    for exit_dt, trade in sorted(closed, key=lambda t: t[0]):
        pnl = _resolve_pnl(trade)
        if pnl is None or pnl == 0:
            continue
        outcome = "win" if pnl > 0 else "loss"
        if streak_type is None or streak_type != outcome:
            streak_type = outcome
            streak_count = 1
        else:
            streak_count += 1
    if streak_type is None:
        return None
    return {"type": streak_type, "count": streak_count}


def _compute_metrics(
    rows: List[Dict[str, Any]], range_key: str, tz, open_positions: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    now_tz = datetime.now(tz)
    start_today = now_tz.replace(hour=0, minute=0, second=0, microsecond=0)
    start_7d = now_tz - timedelta(days=7)

    today_trades = _filter_closed_trades(rows, start_today, tz)
    week_trades = _filter_closed_trades(rows, start_7d, tz)

    def summarize(trades: List[Tuple[datetime, Dict[str, Any]]]) -> Dict[str, Any]:
        if not trades:
            return {
                "net_pnl": 0,
                "gross_pnl": 0,
                "fees": 0,
                "trade_count": 0,
                "win_rate": None,
                "avg_trade": None,
                "profit_factor": None,
            }
        net_total = 0.0
        gross_total = 0.0
        fees_total = 0.0
        wins = 0
        losses = 0
        for _, trade in trades:
            pnl = _resolve_pnl(trade) or 0.0
            net_total += pnl
            gross_val = trade.get("total_pnl")
            try:
                gross_val = float(gross_val) if gross_val is not None else pnl
            except Exception:
                gross_val = pnl
            gross_total += gross_val
            fee_val = trade.get("fees_total")
            try:
                fees_total += float(fee_val) if fee_val is not None else 0.0
            except Exception:
                fees_total += 0.0
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
        trade_count = len(trades)
        gross_wins = sum(
            max((t.get("total_pnl") if t.get("total_pnl") is not None else _resolve_pnl(t)) or 0.0, 0.0)
            for _, t in trades
        )
        gross_losses = sum(
            min((t.get("total_pnl") if t.get("total_pnl") is not None else _resolve_pnl(t)) or 0.0, 0.0)
            for _, t in trades
        )
        win_rate = wins / trade_count if trade_count else None
        avg_trade = net_total / trade_count if trade_count else None
        return {
            "net_pnl": net_total,
            "gross_pnl": gross_total,
            "fees": fees_total,
            "trade_count": trade_count,
            "win_rate": win_rate,
            "avg_trade": avg_trade,
            "profit_factor": _compute_profit_factor(gross_wins, gross_losses) if trade_count else None,
        }

    today_summary = summarize(today_trades)
    week_summary = summarize(week_trades)
    streak = _streak_for_trades(today_trades)

    open_active = [p for p in (open_positions or []) if p.get("has_position")]
    open_unrealized = sum(float(p.get("unrealized_pnl") or 0) for p in open_active)
    open_size = sum(int(p.get("size") or 0) for p in open_active)
    open_side = ", ".join(sorted({p.get("side") for p in open_active if p.get("side")})) or None
    max_duration = max((float(p.get("duration_minutes") or 0) for p in open_active), default=0)

    return {
        "open_positions": {
            "unrealized_pnl": open_unrealized,
            "side": open_side,
            "size": open_size,
            "duration_minutes": max_duration,
        },
        "today": today_summary,
        "seven_day": {
            "net_pnl": week_summary["net_pnl"],
            "win_rate": week_summary["win_rate"],
        },
        "streak": streak,
        "range": range_key,
    }


# ─── Position Snapshot ───────────────────────────────────────────────────────

def _fetch_open_positions_snapshot(account: str = "all") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    cid = get_contract("MES")
    open_positions: List[Dict[str, Any]] = []
    total_unrealized = 0.0
    accounts = [account] if account != "all" else list(ACCOUNTS.keys())

    for acct_name in accounts:
        acct_id = ACCOUNTS.get(acct_name)
        if acct_id is None:
            continue
        try:
            state = position_manager.get_position_state_light(acct_id, cid)
        except Exception as exc:  # noqa: PERF203
            logger.warning("Failed to fetch position for %s: %s", acct_name, exc)
            state = {
                "has_position": False,
                "size": 0,
                "side": None,
                "entry_price": None,
                "current_price": None,
                "unrealized_pnl": 0,
                "duration_minutes": 0,
            }
        state["account"] = acct_name
        total_unrealized += float(state.get("unrealized_pnl") or 0)
        open_positions.append(state)

    return open_positions, {"total_unrealized_pnl": total_unrealized}


# ─── Payload Builders ────────────────────────────────────────────────────────

def _dashboard_payload(account: str, range_key: str, include_open: bool) -> Dict[str, Any]:
    rows, fetch_error = _fetch_ai_trade_feed(account=account, range_key=range_key, include_open=include_open)
    open_positions, open_totals = _fetch_open_positions_snapshot(account=account)
    metrics = _compute_metrics(rows, range_key, MOUNTAIN_TZ, open_positions=open_positions)

    payload: Dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "open_positions": open_positions,
        "open_totals": open_totals,
        "rows": rows,
    }
    if fetch_error:
        payload["errors"] = fetch_error
    return payload


# ─── Routes ──────────────────────────────────────────────────────────────────

dashboard_path = "/dashboard"


@dashboard_bp.route(dashboard_path)
def dashboard():
    account = request.args.get("account", "all")
    if account == "all":
        account = "all"
    account = account if account in ACCOUNTS or account == "all" else DEFAULT_ACCOUNT
    range_key = request.args.get("range", "7d")
    include_open = request.args.get("include_open", "true").lower() != "false"
    payload = _dashboard_payload(account, range_key, include_open)
    return render_template(
        "dashboard.html",
        payload=payload,
        accounts=list(ACCOUNTS.keys()),
        default_account=account,
        default_range=range_key,
        default_include_open=include_open,
    )


@dashboard_bp.route(f"{dashboard_path}/data")
def dashboard_data():
    account = request.args.get("account", "all")
    if account != "all" and account not in ACCOUNTS:
        account = DEFAULT_ACCOUNT
    range_key = request.args.get("range", "7d")
    include_open = request.args.get("include_open", "true").lower() != "false"
    payload = _dashboard_payload(account, range_key, include_open)
    return jsonify(payload)
