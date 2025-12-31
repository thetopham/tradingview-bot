"""Simple dashboard views for monitoring AI trade decisions and results."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

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


def _fetch_ai_reasons(ids: List[object]) -> Tuple[Dict[object, Dict[str, object]], Optional[str]]:
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
            .select("ai_decision_id,reason,screenshot_url,urls")
            .in_("ai_decision_id", cleaned_ids)
            .execute()
        )
        for row in res.data or []:
            urls = row.get("urls")
            if isinstance(urls, str):
                try:
                    urls = json.loads(urls)
                except json.JSONDecodeError:
                    # Keep the raw string as a potential direct URL fallback
                    pass

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

            screenshot_url = row.get("screenshot_url") or _first_url(urls)
            reasons[row.get("ai_decision_id")] = {
                "reason": row.get("reason") or "",
                "screenshot_url": screenshot_url or "",
            }
    except Exception as e:
        logging.error("Failed to fetch AI reasons: %s", e)
        return reasons, str(e)

    return reasons, None


def _dashboard_payload() -> Dict[str, object]:
    trade_rows, trade_error = _fetch_trade_results()
    active_sessions = _collect_active_sessions()

    ai_ids = [row.get("ai_decision_id") for row in trade_rows] + [row.get("ai_decision_id") for row in active_sessions]
    ai_reasons, reason_error = _fetch_ai_reasons(ai_ids)

    for row in trade_rows:
        reason_details = ai_reasons.get(row.get("ai_decision_id"), {})
        row["reason"] = reason_details.get("reason") or row.get("comment")
        row["screenshot_url"] = reason_details.get("screenshot_url")

    for session in active_sessions:
        reason_details = ai_reasons.get(session.get("ai_decision_id"), {})
        session["reason"] = reason_details.get("reason")
        session["screenshot_url"] = reason_details.get("screenshot_url")

    return {
        "updated_at": datetime.now(CT).isoformat(),
        "active_sessions": active_sessions,
        "trade_results": trade_rows,
        "trade_results_error": trade_error,
        "ai_reason_error": reason_error,
    }


dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard")
def dashboard_view():
    return render_template("dashboard.html", data=_dashboard_payload())


@dashboard_bp.route("/dashboard/data")
def dashboard_data():
    return jsonify(_dashboard_payload())
