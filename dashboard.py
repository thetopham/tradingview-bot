"""Simple dashboard views for monitoring AI trade decisions and results."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from flask import Blueprint, jsonify, render_template

from api import get_supabase_client
from config import load_config
from signalr_listener import trade_meta

config = load_config()
CT = config["CT"]
ACCOUNT_NAME_BY_ID = {v: k for k, v in config["ACCOUNTS"].items()}


def _format_ts(ts: Optional[object]) -> str:
    """Return a readable timestamp for mixed input types."""

    if ts is None:
        return ""

    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts), CT).strftime("%Y-%m-%d %H:%M:%S %Z")

        if isinstance(ts, str):
            return ts

        if hasattr(ts, "astimezone"):
            return ts.astimezone(CT).strftime("%Y-%m-%d %H:%M:%S %Z")
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


def _fetch_trade_results(limit: int = 25) -> Tuple[List[Dict[str, object]], Optional[str]]:
    rows: List[Dict[str, object]] = []
    try:
        supabase = get_supabase_client()
    except Exception as e:
        logging.warning("Dashboard Supabase client unavailable: %s", e)
        return rows, str(e)

    try:
        res = (
            supabase.table("trade_results")
            .select(
                "entry_time,exit_time,symbol,signal,ai_decision_id,total_pnl,account,size,comment,strategy"
            )
            .order("entry_time", desc=True)
            .limit(limit)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        logging.error("Failed to fetch trade_results: %s", e)
        return rows, str(e)

    return rows, None


def _first_url(value: object) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for candidate in value.values():
            found = _first_url(candidate)
            if found:
                return found
        return ""
    if isinstance(value, (list, tuple, set)):
        for item in value:
            found = _first_url(item)
            if found:
                return found
    return ""


def _parse_urls(urls: object) -> object:
    if isinstance(urls, str):
        try:
            return json.loads(urls)
        except json.JSONDecodeError:
            return urls
    return urls


def _fetch_ai_reasons(ids: Iterable[object]) -> Tuple[Dict[object, Dict[str, object]], Optional[str]]:
    """Lookup AI decision details (reason, screenshot) for provided ids."""

    reasons: Dict[object, Dict[str, object]] = {}
    cleaned_ids = [i for i in ids if i not in (None, "", [])]
    if not cleaned_ids:
        return reasons, None

    try:
        supabase = get_supabase_client()
    except Exception as e:
        logging.warning("Dashboard Supabase client unavailable for reasons: %s", e)
        return reasons, str(e)

    try:
        res = (
            supabase.table("ai_trading_log")
            .select("ai_decision_id,reason,urls")
            .in_("ai_decision_id", cleaned_ids)
            .execute()
        )
        for row in res.data or []:
            urls = _parse_urls(row.get("urls"))
            reasons[row.get("ai_decision_id")] = {
                "reason": row.get("reason") or "",
                "screenshot_url": _first_url(urls),
            }
    except Exception as e:
        logging.error("Failed to fetch AI reasons: %s", e)
        return reasons, str(e)

    return reasons, None


def _fetch_ai_decisions(limit: int = 50) -> Tuple[List[Dict[str, object]], Optional[str]]:
    rows: List[Dict[str, object]] = []
    try:
        supabase = get_supabase_client()
    except Exception as e:
        logging.warning("Dashboard Supabase client unavailable for AI decisions: %s", e)
        return rows, str(e)

    try:
        res = (
            supabase.table("ai_trading_log")
            .select("ai_decision_id,timestamp,strategy,signal,symbol,account,size,urls,reason")
            .order("timestamp", desc=True)
            .limit(limit)
            .execute()
        )
        rows = res.data or []
        for row in rows:
            row["timestamp"] = _format_ts(row.get("timestamp"))
            urls = _parse_urls(row.get("urls"))
            row["screenshot_url"] = _first_url(urls)
    except Exception as e:
        logging.error("Failed to fetch AI decisions: %s", e)
        return rows, str(e)

    return rows, None


def _dashboard_payload() -> Dict[str, object]:
    ai_decisions, ai_error = _fetch_ai_decisions()
    trade_rows, trade_error = _fetch_trade_results()
    active_sessions = _collect_active_sessions()

    ai_map: Dict[object, Dict[str, object]] = {
        row.get("ai_decision_id"): {"reason": row.get("reason"), "screenshot_url": row.get("screenshot_url")}
        for row in ai_decisions
        if row.get("ai_decision_id") not in (None, "")
    }

    missing_ids = [
        id_
        for id_ in [row.get("ai_decision_id") for row in trade_rows + active_sessions]
        if id_ not in ai_map and id_ not in (None, "")
    ]
    ai_reasons, reason_error = _fetch_ai_reasons(missing_ids)
    ai_map.update(ai_reasons)

    for row in trade_rows:
        reason_details = ai_map.get(row.get("ai_decision_id"), {})
        row["reason"] = reason_details.get("reason") or row.get("comment")
        row["screenshot_url"] = reason_details.get("screenshot_url")

    for session in active_sessions:
        reason_details = ai_map.get(session.get("ai_decision_id"), {})
        session["reason"] = reason_details.get("reason")
        session["screenshot_url"] = reason_details.get("screenshot_url")

    return {
        "updated_at": datetime.now(CT).isoformat(),
        "ai_decisions": ai_decisions,
        "active_sessions": active_sessions,
        "trade_results": trade_rows,
        "trade_results_error": trade_error,
        "ai_decision_error": ai_error,
        "ai_reason_error": reason_error,
    }


dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard")
def dashboard_view():
    return render_template("dashboard.html", data=_dashboard_payload())


@dashboard_bp.route("/dashboard/data")
def dashboard_data():
    return jsonify(_dashboard_payload())
