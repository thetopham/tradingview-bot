"""Dashboard blueprint for viewing AI trade feed and metrics."""
from datetime import datetime
from typing import Any, Dict, List, Optional

from dateutil import parser
from flask import Blueprint, jsonify, render_template

from api import CT, get_supabase_client


dashboard_bp = Blueprint(
    "dashboard",
    __name__,
    template_folder="templates",
    static_folder="static",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse a timestamp-ish value to a tz-aware datetime."""

    if not value:
        return None

    if isinstance(value, datetime):
        return value if value.tzinfo else CT.localize(value)

    try:
        return parser.isoparse(str(value)).astimezone(CT)
    except Exception:
        return None


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _compute_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    now = datetime.now(CT)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _entry_ts(row: Dict[str, Any]) -> Optional[datetime]:
        return _parse_dt(row.get("entry_time") or row.get("entry") or row.get("created_at"))

    def _exit_ts(row: Dict[str, Any]) -> Optional[datetime]:
        return _parse_dt(row.get("exit_time") or row.get("exit"))

    def _pnl_value(row: Dict[str, Any]) -> float:
        return _to_float(row.get("pnl") or row.get("profit_loss") or row.get("pl"))

    open_rows = [r for r in rows if _exit_ts(r) is None]
    closed_rows = [r for r in rows if _exit_ts(r) is not None]

    open_pnl = sum(_to_float(r.get("open_pnl") or r.get("unrealized_pnl") or r.get("pnl")) for r in open_rows)
    wins = [r for r in closed_rows if _pnl_value(r) > 0]
    win_rate = (len(wins) / len(closed_rows) * 100.0) if closed_rows else 0.0

    daily_rows: List[Dict[str, Any]] = []
    for r in rows:
        ts = _exit_ts(r) or _entry_ts(r)
        if ts and ts >= today_start:
            daily_rows.append(r)
    daily_pnl = sum(_pnl_value(r) for r in daily_rows)

    return {
        "open_positions": len(open_rows),
        "open_pnl": round(open_pnl, 2),
        "win_rate": round(win_rate, 2),
        "daily_pnl": round(daily_pnl, 2),
        "total_trades": len(rows),
        "closed_trades": len(closed_rows),
    }


def _fetch_feed(limit: int = 200) -> Dict[str, Any]:
    supabase = get_supabase_client()

    # Prefer the latest trades first
    response = supabase.table("ai_trade_feed").select("*").order("entry_time", desc=True).limit(limit).execute()
    rows = response.data or []

    metrics = _compute_metrics(rows)
    return {"feed": rows, "metrics": metrics}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@dashboard_bp.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@dashboard_bp.route("/api/dashboard/feed")
def dashboard_feed():
    try:
        payload = _fetch_feed()
        return jsonify(payload)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
