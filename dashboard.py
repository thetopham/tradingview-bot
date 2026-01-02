import logging
from datetime import datetime
from typing import Any, Dict, List, Tuple

from dateutil import parser
from flask import Blueprint, jsonify, render_template, request

from api import CT, get_supabase_client


dashboard_bp = Blueprint("dashboard", __name__)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _derive_pnl(trade: Dict[str, Any]) -> float:
    for key in ("net_pnl", "total_pnl", "trade_pnl"):
        if key in trade and trade[key] is not None:
            value = _safe_float(trade[key])
            return value
    return 0.0


def _parse_timestamp(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        return parser.isoparse(ts)
    except Exception:
        return None


def _load_ai_trades(limit: int = 200) -> Tuple[List[Dict[str, Any]], str | None]:
    """Load AI trades from Supabase, ordered by newest decision time."""
    try:
        client = get_supabase_client()
        response = (
            client.table("ai_trade_feed")
            .select("*")
            .order("decision_time", desc=True)
            .limit(limit)
            .execute()
        )
        if response.error:
            return [], str(response.error)
        return response.data or [], None
    except Exception as exc:  # pragma: no cover - defensive path
        logging.exception("Failed to load ai_trade_feed: %s", exc)
        return [], str(exc)


def _compute_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    today = datetime.now(CT).date()
    daily_pnl = 0.0
    wins = 0
    counted = 0
    open_positions = []

    for trade in trades:
        pnl = _derive_pnl(trade)
        decision_dt = _parse_timestamp(trade.get("decision_time"))
        exit_time = trade.get("exit_time")

        if decision_dt and decision_dt.astimezone(CT).date() == today:
            daily_pnl += pnl
        if pnl != 0:
            counted += 1
            if pnl > 0:
                wins += 1
        if exit_time in (None, ""):
            open_positions.append(trade)

    win_rate = (wins / counted * 100) if counted else 0.0
    open_pnl = sum(_derive_pnl(t) for t in open_positions)

    return {
        "daily_pnl": round(daily_pnl, 2),
        "win_rate": round(win_rate, 2),
        "open_pnl": round(open_pnl, 2),
        "open_positions": len(open_positions),
        "total_trades": len(trades),
        "last_updated": datetime.now(CT).isoformat(),
    }


def _shape_trade(trade: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ai_decision_id": trade.get("ai_decision_id"),
        "decision_time": trade.get("decision_time"),
        "entry_time": trade.get("entry_time"),
        "exit_time": trade.get("exit_time"),
        "account": trade.get("account"),
        "symbol": trade.get("symbol"),
        "signal": trade.get("signal"),
        "size": trade.get("size"),
        "pnl": _derive_pnl(trade),
        "reason": trade.get("reason"),
        "screenshot_url": trade.get("screenshot_url") or trade.get("url"),
    }


@dashboard_bp.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html")


@dashboard_bp.route("/api/ai-trades")
def api_ai_trades():
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200

    trades_raw, error = _load_ai_trades(limit)
    if error:
        return jsonify({"error": error}), 500

    trades = [_shape_trade(t) for t in trades_raw]
    metrics = _compute_metrics(trades_raw)
    return jsonify({"trades": trades, "metrics": metrics})
