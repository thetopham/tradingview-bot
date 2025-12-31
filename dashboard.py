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
DISPLAY_TZ = pytz.timezone("America/Denver")
# Restrict dashboard results to recent history to avoid stale Supabase rows.
RECENT_TRADE_WINDOW_DAYS = 7
ACCOUNT_NAME_BY_ID = {v: k for k, v in config["ACCOUNTS"].items()}


def _format_ts(ts: Optional[object]) -> str:
    """Return a readable timestamp for mixed input types."""

    if ts is None:
        return ""

    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(float(ts), DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

        if isinstance(ts, str):
            try:
                parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if parsed.tzinfo:
                    return parsed.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
            except ValueError:
                pass
            return ts

        if hasattr(ts, "astimezone"):
            return ts.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        logging.exception("Failed to format timestamp %s", ts)

    return str(ts)


def _normalize_symbol(_: object) -> str:
    """Force MES display per trading scope."""

    return "MES"


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
                "symbol": _normalize_symbol(meta.get("symbol") or cid),
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


def _fetch_trade_results(limit: int = 25) -> Tuple[List[Dict[str, object]], Optional[str]]:
    rows: List[Dict[str, object]] = []
    try:
        supabase = get_supabase_client()
    except Exception as e:
        logging.warning("Dashboard Supabase client unavailable: %s", e)
        return rows, str(e)

    try:
        cutoff = datetime.now(DISPLAY_TZ) - timedelta(days=RECENT_TRADE_WINDOW_DAYS)
        res = (
            supabase.table("trade_results")
            .select(
                "entry_time,exit_time,symbol,signal,ai_decision_id,total_pnl,account,size,comment,strategy"
            )
            .gte("entry_time", cutoff.isoformat())
            .order("entry_time", desc=True)
            .limit(limit)
            .execute()
        )
        rows = res.data or []
        for row in rows:
            row["symbol"] = _normalize_symbol(row.get("symbol"))
    except Exception as e:
        logging.error("Failed to fetch trade_results: %s", e)
        return rows, str(e)

    return rows, None


def _fetch_ai_decisions(limit: int = 50) -> Tuple[List[Dict[str, object]], Optional[str]]:
    """Retrieve recent AI trading log decisions with parsed screenshot URLs."""

    rows: List[Dict[str, object]] = []
    try:
        supabase = get_supabase_client()
    except Exception as e:
        logging.warning("Dashboard Supabase client unavailable for ai_trading_log: %s", e)
        return rows, str(e)

    try:
        res = (
            supabase.table("ai_trading_log")
            .select("ai_decision_id,timestamp,strategy,signal,symbol,account,size,urls,reason")
            .order("timestamp", desc=True)
            .limit(limit)
            .execute()
        )

        for row in res.data or []:
            urls = row.get("urls")
            if isinstance(urls, str):
                try:
                    urls = json.loads(urls)
                except json.JSONDecodeError:
                    pass

            row["screenshot_url"] = _extract_first_url(urls)
            row["decision_time"] = _format_ts(row.get("timestamp"))
            row["symbol"] = _normalize_symbol(row.get("symbol"))
            rows.append(row)
    except Exception as e:
        logging.error("Failed to fetch ai_trading_log: %s", e)
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
            .select("ai_decision_id,reason,urls")
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

            reasons[row.get("ai_decision_id")] = {
                "reason": row.get("reason") or "",
                "screenshot_url": _extract_first_url(urls),
            }
    except Exception as e:
        logging.error("Failed to fetch AI reasons: %s", e)
        return reasons, str(e)

    return reasons, None


def _dashboard_payload() -> Dict[str, object]:
    trade_rows, trade_error = _fetch_trade_results()
    ai_decisions, ai_error = _fetch_ai_decisions()
    active_sessions = _collect_active_sessions()

    ai_ids = [row.get("ai_decision_id") for row in trade_rows + active_sessions]
    decision_lookup = {row.get("ai_decision_id"): row for row in ai_decisions if row.get("ai_decision_id")}
    missing_ids = [i for i in ai_ids if i not in decision_lookup]
    ai_reasons, reason_error = _fetch_ai_reasons(missing_ids)

    trade_lookup = {row.get("ai_decision_id"): row for row in trade_rows if row.get("ai_decision_id")}

    for row in trade_rows:
        decision = decision_lookup.get(row.get("ai_decision_id")) or {}
        reason_details = ai_reasons.get(row.get("ai_decision_id"), {})
        row["entry_time"] = _format_ts(row.get("entry_time"))
        row["exit_time"] = _format_ts(row.get("exit_time"))
        row["reason"] = decision.get("reason") or reason_details.get("reason") or row.get("comment")
        row["screenshot_url"] = decision.get("screenshot_url") or reason_details.get("screenshot_url")

    for session in active_sessions:
        decision = decision_lookup.get(session.get("ai_decision_id")) or {}
        reason_details = ai_reasons.get(session.get("ai_decision_id"), {})
        session["reason"] = decision.get("reason") or reason_details.get("reason")
        session["screenshot_url"] = decision.get("screenshot_url") or reason_details.get("screenshot_url")

    for decision in ai_decisions:
        ai_id = decision.get("ai_decision_id")
        result = trade_lookup.get(ai_id) or {}
        decision["total_pnl"] = result.get("total_pnl")
        decision["result_entry_time"] = _format_ts(result.get("entry_time"))
        decision["result_exit_time"] = _format_ts(result.get("exit_time"))

    return {
        "updated_at": datetime.now(DISPLAY_TZ).isoformat(),
        "active_sessions": active_sessions,
        "trade_results": trade_rows,
        "ai_decisions": ai_decisions,
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
