"""Dashboard blueprint for viewing AI trade decisions and PnL metrics."""

from datetime import datetime, timedelta
import logging
from typing import Any, Dict, List, Optional

import pytz
from dateutil import parser
from flask import Blueprint, jsonify, render_template

from api import get_supabase_client


CT = pytz.timezone("America/Chicago")

dashboard_bp = Blueprint(
    "dashboard",
    __name__,
    template_folder="templates",
    static_folder="static",
)


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return parser.isoparse(ts)
    except Exception:
        logging.warning("Unable to parse timestamp: %s", ts)
        return None


def _compute_metrics(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    now_ct = datetime.now(CT)
    day_start = now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    open_pnl = 0.0
    closed_pnl_values: List[float] = []
    daily_pnl = 0.0

    for row in rows:
        net_pnl = float(row.get("net_pnl") or 0)
        exit_time = _parse_ts(row.get("exit_time"))
        decision_time = _parse_ts(row.get("decision_time"))

        if exit_time is None:
            open_pnl += net_pnl
        else:
            closed_pnl_values.append(net_pnl)

        if decision_time:
            decision_ct = decision_time.astimezone(CT)
            if day_start <= decision_ct < day_end:
                daily_pnl += net_pnl

    wins = [p for p in closed_pnl_values if p > 0]
    win_rate = (len(wins) / len(closed_pnl_values) * 100) if closed_pnl_values else 0.0

    return {
        "open_pnl": round(open_pnl, 2),
        "win_rate": round(win_rate, 2),
        "daily_pnl": round(daily_pnl, 2),
        "closed_trades": len(closed_pnl_values),
    }


@dashboard_bp.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")


@dashboard_bp.route("/dashboard/data")
def dashboard_data():
    try:
        client = get_supabase_client()
        response = (
            client.table("ai_trade_feed")
            .select(
                "ai_decision_id,decision_time,entry_time,exit_time,account,"
                "symbol,signal,strategy,size,reason,screenshot_url,screener_url,"
                "total_pnl,fees_total,net_pnl,trace_id,session_id"
            )
            .order("decision_time", desc=True)
            .limit(200)
            .execute()
        )
        rows = response.data or []
        metrics = _compute_metrics(rows)
        return jsonify({"trades": rows, "metrics": metrics})
    except Exception as exc:  # pragma: no cover - defensive endpoint
        logging.exception("Dashboard data error: %s", exc)
        return jsonify({"error": str(exc)}), 500
