import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from dateutil import parser
from flask import Blueprint, jsonify, render_template

from api import get_supabase_client
from config import load_config

config = load_config()
CT = config["CT"]

dashboard_bp = Blueprint(
    "dashboard", __name__, template_folder="templates", static_folder="static"
)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = parser.isoparse(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _pnl_value(row: Dict[str, Any]) -> float:
    for key in ("net_pnl", "total_pnl"):
        pnl = row.get(key)
        if pnl is None:
            continue
        try:
            return float(pnl)
        except Exception:
            continue
    return 0.0


def _format_row(row: Dict[str, Any]) -> Dict[str, Any]:
    decision_time = _parse_ts(row.get("decision_time"))
    entry_time = _parse_ts(row.get("entry_time"))
    exit_time = _parse_ts(row.get("exit_time"))

    return {
        "ai_decision_id": row.get("ai_decision_id"),
        "decision_time": decision_time.isoformat() if decision_time else None,
        "entry_time": entry_time.isoformat() if entry_time else None,
        "exit_time": exit_time.isoformat() if exit_time else None,
        "account": row.get("account"),
        "symbol": row.get("symbol"),
        "signal": row.get("signal"),
        "size": row.get("size"),
        "strategy": row.get("strategy"),
        "reason": row.get("reason"),
        "screenshot_url": row.get("screenshot_url") or row.get("viz_url"),
        "pnl": _pnl_value(row),
    }


def _compute_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    now_ct = datetime.now(CT)
    start_of_day_ct = now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_day_utc = start_of_day_ct.astimezone(timezone.utc)

    closed = [r for r in rows if r.get("exit_time")]
    wins = [r for r in closed if _pnl_value(r) > 0]
    open_positions = [r for r in rows if not r.get("exit_time")]
    daily_rows = [
        r
        for r in rows
        if (_parse_ts(r.get("decision_time")) or _parse_ts(r.get("entry_time")) or datetime.min.replace(tzinfo=timezone.utc))
        >= start_of_day_utc
    ]

    win_rate = (len(wins) / len(closed)) * 100 if closed else 0.0
    current_open_pnl = sum(_pnl_value(r) for r in open_positions)
    daily_pnl = sum(_pnl_value(r) for r in daily_rows)

    return {
        "current_open_pnl": current_open_pnl,
        "win_rate": win_rate,
        "daily_pnl": daily_pnl,
        "open_positions": len(open_positions),
        "trades_today": len(daily_rows),
    }


@dashboard_bp.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@dashboard_bp.route("/dashboard/data")
def dashboard_data():
    try:
        supabase = get_supabase_client()
        res = (
            supabase.table("ai_trade_feed")
            .select("*")
            .order("ai_decision_id", desc=True)
            .order("decision_time", desc=True)
            .limit(250)
            .execute()
        )
        rows = res.data or []
    except Exception as exc:  # pragma: no cover - defensive logging
        logging.error("Failed to load ai_trade_feed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    formatted = [_format_row(row) for row in rows]
    metrics = _compute_metrics(rows)

    return jsonify({"trades": formatted, "metrics": metrics})
