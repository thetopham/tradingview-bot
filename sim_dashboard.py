"""Read-only account overview for the persistent simulated broker."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hmac
import json
import os
from zoneinfo import ZoneInfo

from api import get_sim_adapter
from flask import Blueprint, Response, jsonify, render_template, request


sim_dashboard_bp = Blueprint("sim_dashboard", __name__, template_folder="templates")
MOUNTAIN = ZoneInfo("America/Denver")
SPLIT_TEST_START = "2026-09-24T07:00:00+00:00"


@sim_dashboard_bp.before_request
def require_dashboard_password():
    password = os.getenv("DASHBOARD_PASSWORD")
    if not password:
        return Response("Dashboard password is not configured", 503)
    auth = request.authorization
    if not auth or not hmac.compare_digest(auth.password or "", password):
        return Response("Unauthorized", 401,
                        {"WWW-Authenticate": 'Basic realm="Simulated accounts"'})
    return None


@sim_dashboard_bp.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


def _local_time(value: str | None) -> str | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(MOUNTAIN).strftime(
        "%b %d, %I:%M %p MT")


def _dashboard_payload() -> dict:
    adapter = get_sim_adapter()
    accounts = adapter.ledger.status()
    now = datetime.now(timezone.utc)
    with closing(adapter.ledger.connection()) as conn:
        decisions = conn.execute(
            "SELECT d.account,d.generation,d.bar_ts,d.available_at,d.decision_json "
            "FROM sim_decision d JOIN sim_account a ON a.name=d.account "
            "AND a.generation=d.generation WHERE d.bar_ts=(SELECT MAX(x.bar_ts) "
            "FROM sim_decision x WHERE x.account=d.account AND x.generation=d.generation)"
        ).fetchall()
        trades = conn.execute(
            "SELECT t.account,t.generation,t.exit_ts,t.direction,t.quantity,"
            "t.net_pnl,t.reason FROM sim_trade t JOIN sim_account a "
            "ON a.name=t.account AND a.generation=t.generation "
            "ORDER BY t.id DESC LIMIT 30"
        ).fetchall()
        by_account = {item["account"]: item for item in accounts}
        pair_counts = {}
        for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
            numeric, vision = by_account.get(name), by_account.get(f"{name}_vision")
            if not numeric or not vision:
                continue
            pair_counts[name] = conn.execute(
                "SELECT COUNT(*) AS matched, COALESCE(SUM(CASE WHEN "
                "json_extract(n.decision_json, '$.signal') = "
                "json_extract(v.decision_json, '$.signal') THEN 1 ELSE 0 END), 0) "
                "AS agreed FROM sim_decision v JOIN sim_decision n ON "
                "n.account=? AND n.generation=? AND n.bar_ts=v.bar_ts "
                "WHERE v.account=? AND v.generation=? "
                "AND v.available_at>=? AND n.available_at>=?",
                (name, numeric["generation"], f"{name}_vision", vision["generation"],
                 SPLIT_TEST_START, SPLIT_TEST_START),
            ).fetchone()
    latest = {row["account"]: row for row in decisions}
    for account in accounts:
        row = latest.get(account["account"])
        decision = json.loads(row["decision_json"]) if row else None
        account["latest_decision"] = {
            "signal": decision.get("signal"), "reason": decision.get("reason"),
            "bar_ts": row["bar_ts"],
            "bar_time": _local_time(row["bar_ts"]),
            "received_time": _local_time(row["available_at"]),
        } if decision else None
        bar_ts = account["last_bar_ts"]
        close_time = (datetime.fromisoformat(bar_ts.replace("Z", "+00:00"))
                      + timedelta(minutes=int(account["execution_timeframe"][:-1]))) if bar_ts else None
        account["last_bar_time"] = _local_time(close_time.isoformat()) if close_time else None
        account["feed_age_minutes"] = max(0, int((now - close_time).total_seconds() // 60)) if close_time else None
        account["unrealized_pnl"] = round(account["equity"] - account["balance"], 2)
        account["position_side"] = (
            "Long" if account["position"] and account["position"]["direction"] == 1
            else "Short" if account["position"] else None)
        account["progress_pct"] = max(0, min(100, round(
            100 * account["net_pnl"] / account["effective_profit_target"])))
    by_name = {account["account"]: account for account in accounts}
    pairs = []
    for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
        numeric, vision = by_name.get(name), by_name.get(f"{name}_vision")
        if not numeric or not vision:
            continue
        numeric_decision = numeric["latest_decision"]
        vision_decision = vision["latest_decision"]
        pairs.append({
            "name": name,
            "timeframe": numeric["timeframe"],
            "numeric_signal": numeric_decision["signal"] if numeric_decision else None,
            "vision_signal": vision_decision["signal"] if vision_decision else None,
            "same_bar": bool(numeric_decision and vision_decision and
                             numeric_decision["bar_ts"] == vision_decision["bar_ts"]),
            "bar_time": (numeric_decision or vision_decision or {}).get("bar_time"),
            "numeric_equity": numeric["equity"],
            "vision_equity": vision["equity"],
            "matched_decisions": pair_counts[name]["matched"],
            "agreed_decisions": pair_counts[name]["agreed"],
        })
    trade_rows = [{**dict(row), "exit_time": _local_time(row["exit_ts"])} for row in trades]
    return {
        "updated_at": now.astimezone(MOUNTAIN).strftime("%b %d, %I:%M:%S %p MT"),
        "accounts": accounts,
        "pairs": pairs,
        "trades": trade_rows,
        "summary": {
            "total": len(accounts),
            "active": sum(a["status"] == "active" and not a["manual_paused"] for a in accounts),
            "passed": sum(a["status"] == "passed" for a in accounts),
            "failed": sum(a["status"] == "failed" for a in accounts),
            "open_positions": sum(a["position"] is not None for a in accounts),
            "net_pnl": round(sum(a["net_pnl"] for a in accounts), 2),
            "equity_change": round(sum(a["net_pnl"] + a["unrealized_pnl"] for a in accounts), 2),
        },
    }


@sim_dashboard_bp.get("/sim/dashboard")
def sim_dashboard():
    return render_template("sim_dashboard.html", payload=_dashboard_payload())


@sim_dashboard_bp.get("/sim/dashboard/data")
def sim_dashboard_data():
    return jsonify(_dashboard_payload())
