"""Simple dashboard views for monitoring AI trade decisions and results."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, render_template
import pytz

from api import get_supabase_client
from config import load_config
from signalr_listener import trade_meta

config = load_config()
CT = config["CT"]
ACCOUNT_NAME_BY_ID = {v: k for k, v in config["ACCOUNTS"].items()}
DASHBOARD_TZ = pytz.timezone("America/Denver")
DASHBOARD_FEED_TABLE = "dashboard_feed"


def _format_ts(ts: Optional[object]) -> str:
    """Return a readable timestamp for mixed input types."""

    if ts is None:
        return ""

    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts), DASHBOARD_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

        if isinstance(ts, str):
            return ts

        if hasattr(ts, "astimezone"):
            return ts.astimezone(DASHBOARD_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        logging.exception("Failed to format timestamp %s", ts)

    return str(ts)


def _collect_active_sessions() -> List[Dict[str, object]]:
    sessions: List[Dict[str, object]] = []

    for (acct_id, cid), meta in trade_meta.items():
        if not isinstance(meta, dict):
            continue

        account_label = ACCOUNT_NAME_BY_ID.get(acct_id, meta.get("account") or acct_id)
        entry_time = meta.get("entry_time")
        sessions.append(
            {
                "account": account_label,
                "symbol": meta.get("symbol") or cid,
                "signal": meta.get("signal", ""),
                "strategy": meta.get("strategy", ""),
                "size": meta.get("size", 0),
                "ai_decision_id": meta.get("ai_decision_id"),
                "entry_time": _format_ts(entry_time),
                "alert": meta.get("alert", ""),
                "trace_id": meta.get("trace_id"),
            }
        )

    def _sort_key(session: Dict[str, object]):
        raw = session.get("entry_time", "")
        return raw if isinstance(raw, str) else str(raw)

    return sorted(sessions, key=_sort_key, reverse=True)


def _extract_first_url(value: object) -> str:
    """Return the first URL-like entry from nested url payloads."""

    if not value:
        return ""

    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        for candidate in value.values():
            found = _extract_first_url(candidate)
            if found:
                return found
        return ""

    if isinstance(value, (list, tuple, set)):
        for item in value:
            found = _extract_first_url(item)
            if found:
                return found

    return ""


def _parse_datetime(ts: object) -> Optional[datetime]:
    """Convert mixed timestamp inputs to aware datetimes."""

    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), pytz.UTC)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else pytz.UTC.localize(ts)
    return None


def _fetch_dashboard_feed(limit: int = 50) -> Tuple[List[Dict[str, object]], Optional[str]]:
    """Load merged decision and trade data from a unified Supabase view."""

    rows: List[Dict[str, object]] = []
    try:
        supabase = get_supabase_client()
    except Exception as e:
        logging.warning("Dashboard Supabase client unavailable: %s", e)
        return rows, str(e)

    try:
        res = (
            supabase.table(DASHBOARD_FEED_TABLE)
            .select(
                "decision_time,entry_time,exit_time,account,symbol,signal,size,total_pnl,ai_decision_id,reason,screenshot_url,urls"
            )
            .order("decision_time", desc=True)
            .limit(limit)
            .execute()
        )

        for row in res.data or []:
            row["decision_time"] = _format_ts(_parse_datetime(row.get("decision_time")))
            row["entry_time"] = _format_ts(_parse_datetime(row.get("entry_time")))
            row["exit_time"] = _format_ts(_parse_datetime(row.get("exit_time")))
            screenshot_payload = row.get("screenshot_url") or row.get("urls")
            row["screenshot_url"] = _extract_first_url(screenshot_payload)
            rows.append(row)
    except Exception as e:
        logging.error("Failed to fetch merged dashboard feed: %s", e)
        return rows, str(e)

    return rows, None


def _dashboard_payload() -> Dict[str, object]:
    feed_rows, feed_error = _fetch_dashboard_feed()
    active_sessions = _collect_active_sessions()

    return {
        "updated_at": datetime.now(CT).isoformat(),
        "active_sessions": active_sessions,
        "dashboard_feed": feed_rows,
        "dashboard_feed_error": feed_error,
    }


dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard")
def dashboard_view():
    return render_template("dashboard.html", data=_dashboard_payload())


@dashboard_bp.route("/dashboard/data")
def dashboard_data():
    return jsonify(_dashboard_payload())
