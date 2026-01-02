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
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                return ts

            if dt.tzinfo is None:
                dt = pytz.UTC.localize(dt)

            return dt.astimezone(DASHBOARD_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")

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
        return value.strip()

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
        res = (
            supabase.table("trade_results")
            .select(
                "entry_time,exit_time,symbol,signal,ai_decision_id,total_pnl,account,size,comment,strategy"
            )
            .order("entry_time", desc=True)
            .limit(limit * 4)  # Grab extra rows to filter locally
            .execute()
        )
        raw_rows = res.data or []

        now_utc = datetime.now(pytz.UTC)
        cutoff = now_utc - timedelta(days=7)
        future_limit = now_utc + timedelta(hours=12)
        filtered: List[Dict[str, object]] = []

        def _parse_ts(ts: object) -> Optional[datetime]:
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

        for row in raw_rows:
            entry_dt = _parse_ts(row.get("entry_time"))
            exit_dt = _parse_ts(row.get("exit_time"))

            if not entry_dt:
                continue

            entry_utc = entry_dt.astimezone(pytz.UTC)
            if entry_utc < cutoff or entry_utc > future_limit:
                continue

            row["entry_time"] = entry_dt.astimezone(DASHBOARD_TZ)
            row["exit_time"] = exit_dt.astimezone(DASHBOARD_TZ) if exit_dt else None
            filtered.append(row)

        filtered.sort(key=lambda r: r.get("entry_time") or datetime.min, reverse=True)
        rows = filtered[:limit]
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


def _fetch_merged_feed(limit: int = 50) -> Tuple[List[Dict[str, object]], Optional[str]]:
    """Retrieve pre-merged AI trade feed if the Supabase view/table exists.

    Preferred source is the `ai_trade_feed` table 
    """

    rows: List[Dict[str, object]] = []
    try:
        supabase = get_supabase_client()
    except Exception as e:
        logging.warning("Dashboard Supabase client unavailable for ai_trade_feed: %s", e)
        return rows, str(e)

    last_error: Optional[Exception] = None
    for feed_name in ("ai_trade_feed",):
        try:
            res = (
                supabase.table(feed_name)
                .select(
                    "ai_decision_id,decision_time,entry_time,exit_time,account,symbol,signal,size,total_pnl,reason,screenshot_url,strategy"
                )
                .order("decision_time", desc=True)
                .limit(limit)
                .execute()
            )

            for row in res.data or []:
                row["entry_time"] = _format_ts(row.get("entry_time"))
                row["exit_time"] = _format_ts(row.get("exit_time"))
                row["decision_time"] = _format_ts(row.get("decision_time"))
                rows.append(row)

            return rows, None
        except Exception as e:
            last_error = e
            rows = []

    logging.error("Failed to fetch ai_trade_feed from view/table: %s", last_error)
    return rows, str(last_error) if last_error else "unknown error"


def _merge_trades_and_decisions(
    trades: List[Dict[str, object]], ai_decisions: List[Dict[str, object]]
) -> List[Dict[str, object]]:
    """Combine trade results and AI decisions into a unified feed.

    - Prefers trade timestamps for sorting and display when present.
    - Fills missing contextual fields from the AI decision rows (reason, screenshot).
    - Includes AI decisions that do not yet have trade results so the dashboard shows
      the full pipeline in one place.
    """

    decision_lookup = {
        row.get("ai_decision_id"): row for row in ai_decisions if row.get("ai_decision_id")
    }

    def _sortable(ts: object) -> float:
        if isinstance(ts, datetime):
            return ts.timestamp()
        if isinstance(ts, (int, float)):
            return float(ts)
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0.0
        return 0.0

    merged: List[Dict[str, object]] = []
    seen_ids = set()

    for trade in trades:
        ai_id = trade.get("ai_decision_id")
        decision = decision_lookup.get(ai_id, {})

        merged.append(
            {
                "entry_time": _format_ts(trade.get("entry_time")),
                "exit_time": _format_ts(trade.get("exit_time")),
                "decision_time": decision.get("decision_time") or "",
                "account": trade.get("account") or decision.get("account"),
                "symbol": trade.get("symbol") or decision.get("symbol"),
                "signal": trade.get("signal") or decision.get("signal"),
                "size": trade.get("size") if trade.get("size") is not None else decision.get("size"),
                "total_pnl": trade.get("total_pnl"),
                "ai_decision_id": ai_id or decision.get("ai_decision_id"),
                "reason": decision.get("reason") or trade.get("comment") or "",
                "screenshot_url": decision.get("screenshot_url"),
                "strategy": decision.get("strategy") or trade.get("strategy"),
                "_sort_key": _sortable(trade.get("entry_time")),
            }
        )

        if ai_id:
            seen_ids.add(ai_id)

    for decision in ai_decisions:
        ai_id = decision.get("ai_decision_id")
        if ai_id and ai_id in seen_ids:
            continue

        merged.append(
            {
                "entry_time": "",
                "exit_time": "",
                "decision_time": decision.get("decision_time") or "",
                "account": decision.get("account"),
                "symbol": decision.get("symbol"),
                "signal": decision.get("signal"),
                "size": decision.get("size"),
                "total_pnl": None,
                "ai_decision_id": ai_id,
                "reason": decision.get("reason") or "",
                "screenshot_url": decision.get("screenshot_url"),
                "strategy": decision.get("strategy"),
                "_sort_key": _sortable(decision.get("timestamp")),
            }
        )

    merged.sort(key=lambda row: row.get("_sort_key", 0.0), reverse=True)
    for row in merged:
        row.pop("_sort_key", None)

    return merged


def _dashboard_payload() -> Dict[str, object]:
    merged_feed, merged_error = _fetch_merged_feed()
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
        reason_details = ai_reasons.get(ai_id, {})
        decision["total_pnl"] = result.get("total_pnl")
        decision["result_entry_time"] = _format_ts(result.get("entry_time"))
        decision["result_exit_time"] = _format_ts(result.get("exit_time"))
        decision["reason"] = decision.get("reason") or reason_details.get("reason")
        decision["screenshot_url"] = decision.get("screenshot_url") or reason_details.get("screenshot_url")

    if not merged_feed:
        merged_feed = _merge_trades_and_decisions(trade_rows, ai_decisions)
        merged_error = merged_error or trade_error or ai_error or reason_error

    return {
        "updated_at": datetime.now(CT).isoformat(),
        "active_sessions": active_sessions,
        "trade_results": trade_rows,
        "ai_decisions": ai_decisions,
        "merged_trades": merged_feed,
        "merged_error": merged_error,
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
