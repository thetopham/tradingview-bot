"""Simple dashboard views for monitoring AI trade decisions and results."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
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


def _parse_timestamp(ts: object) -> Optional[datetime]:
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


def _fetch_combined_feed(limit: int = 50) -> Tuple[List[Dict[str, object]], Optional[str]]:
    """Retrieve merged trade + AI decision rows from the unified Supabase view."""

    rows: List[Dict[str, object]] = []
    try:
        supabase = get_supabase_client()
    except Exception as e:
        logging.warning("Dashboard Supabase client unavailable: %s", e)
        return rows, str(e)

    try:
        res = (
            supabase.table("ai_trade_feed")
            .select(
                "entry_time,exit_time,account,symbol,signal,size,total_pnl,ai_decision_id,reason,urls"
            )
            .order("entry_time", desc=True)
            .limit(limit * 2)
            .execute()
        )

        now_utc = datetime.now(pytz.UTC)
        cutoff = now_utc - timedelta(days=7)
        future_limit = now_utc + timedelta(hours=12)

        for row in res.data or []:
            entry_dt = _parse_timestamp(row.get("entry_time"))
            exit_dt = _parse_timestamp(row.get("exit_time"))

            if not entry_dt:
                continue

            entry_utc = entry_dt.astimezone(pytz.UTC)
            if entry_utc < cutoff or entry_utc > future_limit:
                continue

            urls = row.get("urls")
            if isinstance(urls, str):
                try:
                    urls = json.loads(urls)
                except json.JSONDecodeError:
                    pass

            row["entry_time"] = entry_dt.astimezone(DASHBOARD_TZ)
            row["exit_time"] = exit_dt.astimezone(DASHBOARD_TZ) if exit_dt else None
            row["screenshot_url"] = _extract_first_url(urls)
            rows.append(row)

        rows.sort(key=lambda r: r.get("entry_time") or datetime.min, reverse=True)
        rows = rows[:limit]
    except Exception as e:
        logging.error("Failed to fetch ai_trade_feed: %s", e)
        return rows, str(e)

    return rows, None


def _dashboard_payload() -> Dict[str, object]:
    feed_rows, feed_error = _fetch_combined_feed()
    active_sessions = _collect_active_sessions()

    for row in feed_rows:
        row["entry_time"] = _format_ts(row.get("entry_time"))
        row["exit_time"] = _format_ts(row.get("exit_time"))

    return {
        "updated_at": datetime.now(CT).isoformat(),
        "active_sessions": active_sessions,
        "combined_feed": feed_rows,
        "combined_feed_error": feed_error,
    }


dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard")
def dashboard_view():
    return render_template("dashboard.html", data=_dashboard_payload())


@dashboard_bp.route("/dashboard/data")
def dashboard_data():
    return jsonify(_dashboard_payload())
