"""Dashboard blueprint for AI trade feed and trader metrics."""
from datetime import datetime, date
import logging
from typing import Any, Dict, List, Optional

import pytz
from dateutil import parser as date_parser
from flask import Blueprint, jsonify, render_template

from api import get_supabase_client
from config import load_config

config = load_config()
CT = config["CT"]

dashboard_bp = Blueprint("dashboard", __name__)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
        if dt.tzinfo is None:
            dt = pytz.UTC.localize(dt)
        return dt.astimezone(CT).date()
    except Exception:
        return None


def fetch_ai_trade_feed(limit: int = 200) -> List[Dict[str, Any]]:
    """Fetch the latest AI trade feed rows from Supabase."""
    try:
        supabase = get_supabase_client()
    except Exception as exc:  # pragma: no cover - runtime env guard
        logging.error("Supabase client unavailable: %s", exc)
        return []
    # Attempt primary ordering by decision_time, fallback to created_at
    for order_column in ("decision_time", "created_at", "inserted_at"):
        try:
            result = (
                supabase
                .table("ai_trade_feed")
                .select("*")
                .order(order_column, desc=True)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as exc:  # pragma: no cover - defensive logging only
            logging.debug("Failed ordering by %s: %s", order_column, exc)
    try:
        result = supabase.table("ai_trade_feed").select("*").limit(limit).execute()
        return result.data or []
    except Exception as exc:  # pragma: no cover
        logging.error("Unable to fetch ai_trade_feed: %s", exc)
        return []


def compute_metrics(feed: List[Dict[str, Any]]) -> Dict[str, Any]:
    today = datetime.now(CT).date()

    def is_closed(row: Dict[str, Any]) -> bool:
        return bool(row.get("exit") or row.get("exit_time") or row.get("exit_price"))

    closed = [row for row in feed if is_closed(row)]
    wins = [row for row in closed if _safe_float(row.get("pnl")) > 0]

    daily_pnl = 0.0
    for row in feed:
        decision_date = _parse_date(row.get("decision_time") or row.get("entry_time"))
        if decision_date and decision_date == today:
            daily_pnl += _safe_float(row.get("pnl"))

    open_positions = [row for row in feed if not is_closed(row) and row.get("pnl") is not None]
    open_pnl = sum(_safe_float(row.get("pnl")) for row in open_positions)

    win_rate = (len(wins) / len(closed) * 100) if closed else 0.0

    return {
        "total_trades": len(feed),
        "closed_trades": len(closed),
        "wins": len(wins),
        "win_rate": round(win_rate, 2),
        "daily_pnl": round(daily_pnl, 2),
        "open_position_pnl": round(open_pnl, 2),
    }


@dashboard_bp.route("/dashboard", methods=["GET"])
def dashboard():
    return render_template("dashboard.html")


@dashboard_bp.route("/api/dashboard/feed", methods=["GET"])
def dashboard_feed():
    feed = fetch_ai_trade_feed()
    metrics = compute_metrics(feed)
    return jsonify({"feed": feed, "metrics": metrics})
