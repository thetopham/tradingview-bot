import logging
from datetime import datetime, timedelta

from dateutil import parser
from flask import Blueprint, jsonify, render_template

from api import get_supabase_client
from config import load_config

config = load_config()
CT = config["CT"]


dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard")
def dashboard_page():
    """Render the dashboard shell; data is fetched via JS."""
    return render_template("dashboard.html")


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = parser.parse(value)
        if dt.tzinfo is None:
            dt = CT.localize(dt)
        return dt.astimezone(CT)
    except Exception:
        return None


def _float_or_zero(value):
    try:
        return float(value)
    except Exception:
        return 0.0


def _compute_metrics(rows):
    now = datetime.now(CT)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_today = start_today + timedelta(days=1)

    closed_trades = 0
    wins = 0
    losses = 0
    open_positions = 0
    open_pnl = 0.0
    daily_pnl = 0.0

    for row in rows:
        decision_time = _parse_dt(row.get("decision_time"))
        exit_time = _parse_dt(row.get("exit_time"))

        pnl_value = row.get("net_pnl")
        if pnl_value is None:
            pnl_value = row.get("total_pnl")
        pnl = _float_or_zero(pnl_value)

        if exit_time:
            closed_trades += 1
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
        else:
            open_positions += 1
            open_pnl += pnl

        if decision_time and start_today <= decision_time < end_today:
            daily_pnl += pnl

    win_rate = (wins / closed_trades * 100) if closed_trades else None

    return {
        "open_positions": open_positions,
        "open_pnl": round(open_pnl, 2),
        "daily_pnl": round(daily_pnl, 2),
        "closed_trades": closed_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1) if win_rate is not None else None,
    }


@dashboard_bp.route("/dashboard/data")
def dashboard_data():
    """Return merged AI trade feed + day-trader style metrics."""
    try:
        supabase = get_supabase_client()
        result = (
            supabase
            .table("ai_trade_feed")
            .select(
                "ai_decision_id, decision_time, entry_time, exit_time, entry, exit, "
                "account, symbol, signal, size, net_pnl, total_pnl, reason, screenshot_url, url"
            )
            .order("ai_decision_id", desc=True)
            .limit(200)
            .execute()
        )
        rows = result.data or []
    except Exception as exc:
        logging.error("Error loading ai_trade_feed: %s", exc)
        return jsonify({"error": str(exc)}), 500

    metrics = _compute_metrics(rows)
    return jsonify({"trades": rows, "metrics": metrics})
