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
                "entry_time,exit_time,duration_sec_text,duration_sec,total_pnl,raw_trades,order_id,trade_ids,"
                "symbol,signal,ai_decision_id,account,size,comment,strategy"
            )
            .order("entry_time", desc=True)
            .limit(limit)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        logging.error("Failed to fetch trade_results: %s", e)
        return rows, str(e)

    for row in rows:
        row["entry_time"] = _format_ts(row.get("entry_time"))
        row["exit_time"] = _format_ts(row.get("exit_time"))

        if "duration" not in row:
            duration_val = row.get("duration_sec_text") or row.get("duration_sec")
            row["duration"] = duration_val
        else:
            row["duration"] = row.get("duration")

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

    def _parse_urls(urls_value: object) -> object:
        """Return JSON-decoded urls column when it is a string."""

        if isinstance(urls_value, str):
            try:
                return json.loads(urls_value)
            except json.JSONDecodeError:
                return urls_value
        return urls_value

    try:
        # Only request known columns to avoid missing-column errors (e.g., screenshot_url).
        res = (
            supabase.table("ai_trading_log")
            .select("ai_decision_id,timestamp,strategy,signal,symbol,account,size,urls,reason")
            .in_("ai_decision_id", cleaned_ids)
            .execute()
        )
        for row in res.data or []:
            urls = _parse_urls(row.get("urls"))
            screenshot_url = _first_url(urls)

            reasons[row.get("ai_decision_id")] = {
                "reason": row.get("reason") or "",
                "screenshot_url": screenshot_url or "",
                "symbol": row.get("symbol") or "MES",
                "strategy": row.get("strategy"),
                "signal": row.get("signal"),
                "account": row.get("account"),
                "size": row.get("size"),
                "created_at": row.get("timestamp"),
            }
    except Exception as e:
        logging.error("Failed to fetch AI reasons: %s", e)
        return reasons, str(e)

    return reasons, None


def _summarize_trades(trade_rows: List[Dict[str, object]]) -> Dict[str, object]:
    total_pnl = 0.0
    wins = 0
    losses = 0
    max_gain = None
    max_loss = None

    for row in trade_rows:
        pnl = row.get("total_pnl")
        try:
            num = float(pnl)
        except (TypeError, ValueError):
            continue
        total_pnl += num
        if num >= 0:
            wins += 1
            max_gain = num if max_gain is None else max(max_gain, num)
        else:
            losses += 1
            max_loss = num if max_loss is None else min(max_loss, num)

    total = wins + losses
    win_rate = (wins / total * 100) if total else 0.0

    return {
        "total_pnl": total_pnl,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "max_gain": max_gain,
        "max_loss": max_loss,
    }


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

    ai_journal = list(ai_reasons.values())

    def _journal_sort(item: Dict[str, object]):
        ts = item.get("created_at")
        try:
            return datetime.fromisoformat(ts) if isinstance(ts, str) else ts or datetime.min
        except Exception:
            return datetime.min

    ai_journal = sorted(ai_journal, key=_journal_sort, reverse=True)

    return {
        "updated_at": datetime.now(CT).isoformat(),
        "active_sessions": active_sessions,
        "trade_results": trade_rows,
        "trade_results_error": trade_error,
        "ai_reason_error": reason_error,
        "trade_summary": _summarize_trades(trade_rows),
        "ai_journal": ai_journal,
    }


dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard")
def dashboard_view():
    return render_template("dashboard.html", data=_dashboard_payload())


@dashboard_bp.route("/dashboard/data")
def dashboard_data():
    return jsonify(_dashboard_payload())
