"""Dashboard blueprint for AI day trader feed and metrics."""

import logging
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


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), CT)
        if isinstance(value, str):
            return parser.isoparse(value).astimezone(CT)
        if isinstance(value, datetime):
            return value if value.tzinfo else CT.localize(value)
    except Exception:
        return None
    return None


def _first_present(data: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key in data and data.get(key) not in (None, ""):
            return data.get(key)
    return None


def _pnl_value(trade: Dict[str, Any]) -> Optional[float]:
    pnl_keys = ["pnl", "pnl_realized", "realized_pnl", "net_pnl", "pnl_net"]
    for key in pnl_keys:
        value = trade.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def _unrealized_pnl(trade: Dict[str, Any]) -> float:
    unrealized_keys = ["unrealized_pnl", "pnl_open", "open_pnl"]
    for key in unrealized_keys:
        value = trade.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except Exception:
            continue
    return 0.0


def _has_exit(trade: Dict[str, Any]) -> bool:
    exit_keys = ["exit_time", "exit_at", "exit", "closed_at"]
    return _first_present(trade, exit_keys) is not None


def _fetch_feed(limit: int = 200) -> List[Dict[str, Any]]:
    try:
        supabase = get_supabase_client()
        response = (
            supabase.table("ai_trade_feed")
            .select("*")
            .order("entry_time", desc=True)
            .limit(limit)
            .execute()
        )
        data = getattr(response, "data", None)
        if isinstance(data, list):
            return data
        logging.warning("Unexpected Supabase response format: %s", response)
        return []
    except Exception as exc:
        logging.error("Failed to fetch ai_trade_feed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@dashboard_bp.route("/dashboard")
def dashboard() -> str:
    return render_template("dashboard.html")


@dashboard_bp.route("/api/dashboard/feed")
def dashboard_feed():
    data = _fetch_feed()
    return jsonify({"data": data, "count": len(data)})


@dashboard_bp.route("/api/dashboard/metrics")
def dashboard_metrics():
    data = _fetch_feed(limit=500)
    today = datetime.now(CT).date()

    closed_today: List[Dict[str, Any]] = []
    for trade in data:
        exit_raw = _first_present(trade, ["exit_time", "exit_at", "exit", "closed_at"])
        exit_ts = _parse_ts(exit_raw)
        if exit_ts and exit_ts.date() == today:
            closed_today.append(trade)

    pnl_values = [p for p in (_pnl_value(t) for t in closed_today) if p is not None]
    daily_pnl = round(sum(pnl_values), 2) if pnl_values else 0.0
    wins = len([p for p in pnl_values if p > 0])
    total_closed = len(pnl_values)
    win_rate = round((wins / total_closed) * 100, 1) if total_closed else 0.0

    open_positions = [t for t in data if not _has_exit(t)]
    open_pnl = round(sum(_unrealized_pnl(t) for t in open_positions), 2)

    metrics = {
        "daily_pnl": daily_pnl,
        "win_rate": win_rate,
        "wins": wins,
        "closed_trades": total_closed,
        "open_positions": len(open_positions),
        "open_pnl": open_pnl,
        "as_of": datetime.now(CT).isoformat(),
    }
    return jsonify(metrics)
