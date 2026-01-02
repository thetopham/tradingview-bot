"""Dashboard blueprint for AI trading feed and metrics."""
from datetime import datetime
import logging
from typing import Any, Dict, List, Optional

from dateutil import parser
from flask import Blueprint, jsonify, render_template

from api import get_supabase_client
from config import load_config

config = load_config()
CT = config["CT"]


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return parser.parse(value)
    except Exception:
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_ai_trade_feed(limit: int = 200) -> List[Dict[str, Any]]:
    supabase = get_supabase_client()
    response = (
        supabase.table("ai_trade_feed")
        .select("*")
        .order("entry_time", desc=True)
        .limit(limit)
        .execute()
    )
    if hasattr(response, "data"):
        return response.data or []
    if isinstance(response, dict):
        return response.get("data") or []
    return []


def _compute_day_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    now = datetime.now(CT)
    start_of_day = datetime(now.year, now.month, now.day, tzinfo=CT)
    daily_trades: List[Dict[str, Any]] = []

    for trade in trades:
        entry_time = _parse_ts(trade.get("entry_time") or trade.get("entry"))
        if entry_time and entry_time >= start_of_day:
            daily_trades.append(trade)

    closed = [t for t in daily_trades if t.get("exit_time") or t.get("exit")]
    closed_pnls = [
        _safe_float(t.get("pnl"))
        for t in closed
        if _safe_float(t.get("pnl")) is not None
    ]

    daily_pnl = sum(closed_pnls) if closed_pnls else 0.0
    wins = [p for p in closed_pnls if p > 0]
    win_rate = (len(wins) / len(closed_pnls) * 100) if closed_pnls else 0.0

    open_trades = [t for t in daily_trades if not (t.get("exit_time") or t.get("exit"))]
    open_pnl_values = []
    for t in open_trades:
        pnl_value = _safe_float(t.get("pnl"))
        if pnl_value is None:
            pnl_value = _safe_float(t.get("open_pnl") or t.get("unrealized_pnl"))
        if pnl_value is not None:
            open_pnl_values.append(pnl_value)
    open_pnl = sum(open_pnl_values) if open_pnl_values else 0.0

    return {
        "daily_pnl": daily_pnl,
        "win_rate": win_rate,
        "open_position_pnl": open_pnl,
        "trades_today": len(daily_trades),
        "closed_trades_today": len(closed_pnls),
    }


def _serialize_trade(trade: Dict[str, Any]) -> Dict[str, Any]:
    entry_time = _parse_ts(trade.get("entry_time") or trade.get("entry"))
    exit_time = _parse_ts(trade.get("exit_time") or trade.get("exit"))

    return {
        "entry_time": entry_time.isoformat() if entry_time else None,
        "exit_time": exit_time.isoformat() if exit_time else None,
        "account": trade.get("account"),
        "symbol": trade.get("symbol"),
        "signal": trade.get("signal"),
        "size": trade.get("size"),
        "pnl": _safe_float(trade.get("pnl")),
        "ai_decision_id": trade.get("ai_decision_id") or trade.get("ai_id"),
        "reason": trade.get("reason"),
        "screenshot": trade.get("screenshot") or trade.get("screenshot_url"),
        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
    }


dashboard_bp = Blueprint(
    "dashboard",
    __name__,
    template_folder="templates",
    static_folder="static",
)


@dashboard_bp.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@dashboard_bp.route("/dashboard/api/feed")
def dashboard_feed():
    try:
        trades = _fetch_ai_trade_feed()
    except Exception as exc:  # pragma: no cover - defensive API guard
        logging.exception("Failed to fetch ai_trade_feed: %s", exc)
        return jsonify({"error": "Failed to load ai_trade_feed"}), 500

    metrics = _compute_day_metrics(trades)
    serialized_trades = [_serialize_trade(t) for t in trades]

    return jsonify({"trades": serialized_trades, "metrics": metrics})
