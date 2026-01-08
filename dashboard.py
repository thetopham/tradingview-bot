import json
import logging
import pytz
import time

try:
    import httpx
except Exception:
    httpx = None
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from dateutil import parser
from api import get_contract, get_supabase_client, search_accounts
from config import load_config
from position_manager import PositionManager
from flask import Blueprint, Response, jsonify, render_template, request, send_from_directory, abort
from pathlib import Path


config = load_config()
ACCOUNTS = config["ACCOUNTS"]
DEFAULT_ACCOUNT = config["DEFAULT_ACCOUNT"]
DASHBOARD_PASSWORD = config.get("DASHBOARD_PASSWORD")
MOUNTAIN_TZ = pytz.timezone("America/Denver")

logger = logging.getLogger(__name__)


dashboard_bp = Blueprint("dashboard", __name__, template_folder="templates", static_folder="static")
position_manager = PositionManager(ACCOUNTS)


def _dashboard_requires_auth() -> bool:
    return bool(DASHBOARD_PASSWORD)


def _check_dashboard_auth() -> bool:
    if not _dashboard_requires_auth():
        return True
    auth = request.authorization
    if not auth or not auth.password:
        return False
    return auth.password == DASHBOARD_PASSWORD


@dashboard_bp.before_request
def _require_dashboard_auth() -> Optional[Response]:
    if _check_dashboard_auth():
        return None
    return Response(
        "Unauthorized",
        401,
        {"WWW-Authenticate": 'Basic realm="Dashboard"'},
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_json_loads(val: Any) -> Optional[Any]:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return None
    return None


def _looks_like_url(candidate: Any) -> bool:
    if not isinstance(candidate, str):
        return False
    return candidate.startswith("http://") or candidate.startswith("https://")


def _extract_first_url(candidate: Any) -> Optional[str]:
    if candidate is None:
        return None
    if isinstance(candidate, str):
        return candidate if _looks_like_url(candidate) else None
    if isinstance(candidate, dict):
        for _, value in candidate.items():
            found = _extract_first_url(value)
            if found:
                return found
    if isinstance(candidate, list):
        for value in candidate:
            found = _extract_first_url(value)
            if found:
                return found
    return None


def _resolve_reason(record: Dict[str, Any]) -> Optional[str]:
    reason = record.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()

    decision_json = record.get("decision_json") or {}
    if isinstance(decision_json, str):
        decision_json = _safe_json_loads(decision_json) or {}

    if isinstance(decision_json, dict):
        reason = decision_json.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    return None


def _resolve_exit_reason(record: Dict[str, Any]) -> Optional[str]:
    reason = record.get("exit_reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return None


def _is_flatten_exit(record: Dict[str, Any]) -> bool:
    exit_signal = record.get("exit_signal")
    if isinstance(exit_signal, str) and exit_signal.strip().upper() == "FLAT":
        return True
    exit_trigger = record.get("exit_trigger")
    if isinstance(exit_trigger, str) and "flatten" in exit_trigger.lower():
        return True
    return False


def _resolve_screenshot(record: Dict[str, Any]) -> Optional[str]:
    screenshot_url = record.get("screenshot_url") or None
    if isinstance(screenshot_url, str) and screenshot_url.strip():
        screenshot_url = screenshot_url.strip()
        if _looks_like_url(screenshot_url):
            return screenshot_url

    urls = record.get("urls")
    parsed_urls = _safe_json_loads(urls)
    if parsed_urls is not None:
        found = _extract_first_url(parsed_urls)
        if found:
            return found
    if isinstance(urls, str) and _looks_like_url(urls.strip()):
        return urls.strip()

    decision_json = record.get("decision_json") or {}
    if isinstance(decision_json, str):
        decision_json = _safe_json_loads(decision_json) or {}
    if isinstance(decision_json, dict):
        found = _extract_first_url(decision_json.get("urls"))
        if found:
            return found
        found = decision_json.get("screenshot_url") or decision_json.get("screenshot")
        if _looks_like_url(found):
            return found
    return None


def _coerce_dt(raw_val: Any) -> Optional[datetime]:
    if raw_val is None:
        return None
    if isinstance(raw_val, datetime):
        if raw_val.tzinfo is None:
            return raw_val.replace(tzinfo=timezone.utc)
        return raw_val
    try:
        dt = parser.parse(str(raw_val))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _range_start_iso(range_key: str) -> Optional[str]:
    now_mt = datetime.now(MOUNTAIN_TZ)
    if range_key == "today":
        start_mt = _trading_day_start(now_mt, MOUNTAIN_TZ)
    elif range_key == "30d":
        start_mt = now_mt - timedelta(days=30)
    else:
        start_mt = now_mt - timedelta(days=7)
    return start_mt.astimezone(timezone.utc).isoformat()


def _trading_day_start(now_tz: datetime, tz) -> datetime:
    start_today = now_tz.astimezone(tz).replace(hour=16, minute=0, second=0, microsecond=0)
    if now_tz < start_today:
        return start_today - timedelta(days=1)
    return start_today


def _resolve_pnl(record: Dict[str, Any]) -> Optional[float]:
    pnl = record.get("net_pnl")
    if pnl is None:
        pnl = record.get("total_pnl")
    try:
        return float(pnl) if pnl is not None else None
    except Exception:
        return None


def _compute_profit_factor(gross_wins: float, gross_losses: float) -> Optional[float]:
    if gross_losses < 0:
        gross_losses = abs(gross_losses)
    if gross_losses == 0:
        return None
    return gross_wins / gross_losses


# ─── Supabase Fetch ───────────────────────────────────────────────────────────

def _fetch_ai_trade_feed(
    *, limit: int = 5000, account: str = "all", range_key: str = "7d", include_open: bool = True
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, str]]]:
    errors: Optional[Dict[str, str]] = None
    try:
        sb = get_supabase_client()
        columns = (
            "ai_decision_id,decision_time,entry_time,exit_time,account,symbol,signal,size,"
            "strategy,reason,screenshot_url,urls,total_pnl,fees_total,net_pnl,"
            "entry_price,exit_price,decision_json,updated_at,exit_ai_decision_id,exit_reason,"
            "exit_signal,exit_trigger"
        )
        query = sb.table("ai_trade_feed").select(columns)
        if account != "all":
            query = query.eq("account", account)

        start_iso = _range_start_iso(range_key)
        if start_iso:
            query = query.gte("decision_time", start_iso)

        query = query.order("decision_time", desc=True).order("ai_decision_id", desc=True).limit(limit)
                # Retry transient network/protocol hiccups (common with HTTP/2)
        last_exc = None
        for attempt in range(3):
            try:
                resp = query.execute()
                data = resp.data or []
                break
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                retryable = (
                    "server disconnected" in msg
                    or "remoteprotocolerror" in msg
                    or (httpx and isinstance(exc, (
                        httpx.RemoteProtocolError,
                        httpx.ReadError,
                        httpx.ConnectError,
                        httpx.TimeoutException,
                    )))
                )
                if attempt < 2 and retryable:
                    logger.warning(
                        "ai_trade_feed fetch failed (%s). Retrying %d/3...",
                        exc, attempt + 1
                    )
                    # Drop any stale pooled connection by rebuilding the client
                    from api import reset_supabase_client
                    reset_supabase_client()
                    sb = get_supabase_client()

                    # Rebuild the query object (it’s tied to the old client)
                    query = sb.table("ai_trade_feed").select(columns)
                    if account != "all":
                        query = query.eq("account", account)
                    start_iso = _range_start_iso(range_key)
                    if start_iso:
                        query = query.gte("decision_time", start_iso)
                    query = query.order("decision_time", desc=True).order("ai_decision_id", desc=True).limit(limit)

                    time.sleep(0.5 * (2 ** attempt))
                    continue

                raise
        else:
            raise last_exc

        data = resp.data or []
    except Exception as exc:  # noqa: PERF203
        logger.exception("Error fetching ai_trade_feed: %s", exc)
        return [], {"fetch": str(exc)}

    rows: List[Dict[str, Any]] = []
    for record in data:
        exit_time = record.get("exit_time")
        if not include_open and exit_time is None:
            continue

        decision_dt = _coerce_dt(record.get("decision_time")) or _coerce_dt(record.get("updated_at"))
        entry_dt = _coerce_dt(record.get("entry_time"))
        exit_dt = _coerce_dt(record.get("exit_time"))

        resolved = {
            "ai_decision_id": record.get("ai_decision_id"),
            "decision_time": decision_dt.isoformat() if decision_dt else None,
            "entry_time": entry_dt.isoformat() if entry_dt else None,
            "exit_time": exit_dt.isoformat() if exit_dt else None,
            "account": record.get("account"),
            "symbol": record.get("symbol"),
            "signal": record.get("signal"),
            "size": record.get("size"),
            "strategy": record.get("strategy"),
            "pnl": _resolve_pnl(record),
            "net_pnl": record.get("net_pnl"),
            "total_pnl": record.get("total_pnl"),
            "fees_total": record.get("fees_total"),
            "entry_price": record.get("entry_price"),
            "exit_price": record.get("exit_price"),
            "reason": _resolve_reason(record),
            "exit_reason": _resolve_exit_reason(record),
            "exit_ai_decision_id": record.get("exit_ai_decision_id"),
            "exit_signal": record.get("exit_signal"),
            "exit_trigger": record.get("exit_trigger"),
            "is_flatten_exit": _is_flatten_exit(record),
            "screenshot": _resolve_screenshot(record),
        }
        rows.append(resolved)

    rows.sort(
        key=lambda r: (
            _coerce_dt(r.get("decision_time")) or datetime.min.replace(tzinfo=timezone.utc),
            r.get("ai_decision_id") or 0,
        ),
        reverse=True,
    )
    return rows, errors


# ─── Metrics ──────────────────────────────────────────────────────────────────

def _filter_closed_trades(rows: List[Dict[str, Any]], start_dt_tz: datetime, tz) -> List[Tuple[datetime, Dict[str, Any]]]:
    start_utc = start_dt_tz.astimezone(timezone.utc)
    closed: List[Tuple[datetime, Dict[str, Any]]] = []
    for row in rows:
        exit_dt = _coerce_dt(row.get("exit_time"))
        if exit_dt and exit_dt >= start_utc:
            closed.append((exit_dt.astimezone(tz), row))
    return closed


def _streak_for_trades(closed: List[Tuple[datetime, Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    if not closed:
        return None
    streak_type = None
    streak_count = 0
    for exit_dt, trade in sorted(closed, key=lambda t: t[0]):
        pnl = _resolve_pnl(trade)
        if pnl is None or pnl == 0:
            continue
        outcome = "win" if pnl > 0 else "loss"
        if streak_type is None or streak_type != outcome:
            streak_type = outcome
            streak_count = 1
        else:
            streak_count += 1
    if streak_type is None:
        return None
    return {"type": streak_type, "count": streak_count}


def _compute_metrics(
    rows: List[Dict[str, Any]], range_key: str, tz, open_positions: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    now_tz = datetime.now(tz)
    start_today = _trading_day_start(now_tz, tz)
    start_7d = now_tz - timedelta(days=7)

    today_trades = _filter_closed_trades(rows, start_today, tz)
    week_trades = _filter_closed_trades(rows, start_7d, tz)

    def summarize(trades: List[Tuple[datetime, Dict[str, Any]]]) -> Dict[str, Any]:
        if not trades:
            return {
                "net_pnl": 0,
                "gross_pnl": 0,
                "fees": 0,
                "trade_count": 0,
                "win_rate": None,
                "avg_trade": None,
                "profit_factor": None,
            }
        net_total = 0.0
        gross_total = 0.0
        fees_total = 0.0
        wins = 0
        losses = 0
        for _, trade in trades:
            pnl = _resolve_pnl(trade) or 0.0
            net_total += pnl
            gross_val = trade.get("total_pnl")
            try:
                gross_val = float(gross_val) if gross_val is not None else pnl
            except Exception:
                gross_val = pnl
            gross_total += gross_val
            fee_val = trade.get("fees_total")
            try:
                fees_total += float(fee_val) if fee_val is not None else 0.0
            except Exception:
                fees_total += 0.0
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
        trade_count = len(trades)
        gross_wins = sum(
            max((t.get("total_pnl") if t.get("total_pnl") is not None else _resolve_pnl(t)) or 0.0, 0.0)
            for _, t in trades
        )
        gross_losses = sum(
            min((t.get("total_pnl") if t.get("total_pnl") is not None else _resolve_pnl(t)) or 0.0, 0.0)
            for _, t in trades
        )
        win_rate = wins / trade_count if trade_count else None
        avg_trade = net_total / trade_count if trade_count else None
        return {
            "net_pnl": net_total,
            "gross_pnl": gross_total,
            "fees": fees_total,
            "trade_count": trade_count,
            "win_rate": win_rate,
            "avg_trade": avg_trade,
            "profit_factor": _compute_profit_factor(gross_wins, gross_losses) if trade_count else None,
        }

    today_summary = summarize(today_trades)
    week_summary = summarize(week_trades)
    streak = _streak_for_trades(today_trades)

    open_active = [p for p in (open_positions or []) if p.get("has_position")]
    open_unrealized = sum(float(p.get("unrealized_pnl") or 0) for p in open_active)
    open_size = sum(int(p.get("size") or 0) for p in open_active)
    open_side = ", ".join(sorted({p.get("side") for p in open_active if p.get("side")})) or None
    max_duration = max((float(p.get("duration_minutes") or 0) for p in open_active), default=0)

    return {
        "open_positions": {
            "unrealized_pnl": open_unrealized,
            "side": open_side,
            "size": open_size,
            "duration_minutes": max_duration,
        },
        "today": today_summary,
        "seven_day": {
            "net_pnl": week_summary["net_pnl"],
            "win_rate": week_summary["win_rate"],
        },
        "streak": streak,
        "range": range_key,
    }


# ─── Position Snapshot ───────────────────────────────────────────────────────

def _fetch_open_positions_snapshot(account: str = "all") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    cid = get_contract("MES")
    open_positions: List[Dict[str, Any]] = []
    total_unrealized = 0.0
    accounts = [account] if account != "all" else list(ACCOUNTS.keys())

    for acct_name in accounts:
        acct_id = ACCOUNTS.get(acct_name)
        if acct_id is None:
            continue
        try:
            state = position_manager.get_position_state_light(acct_id, cid)
        except Exception as exc:  # noqa: PERF203
            logger.warning("Failed to fetch position for %s: %s", acct_name, exc)
            state = {
                "has_position": False,
                "size": 0,
                "side": None,
                "entry_price": None,
                "current_price": None,
                "unrealized_pnl": 0,
                "duration_minutes": 0,
            }
        state["account"] = acct_name
        total_unrealized += float(state.get("unrealized_pnl") or 0)
        open_positions.append(state)

    return open_positions, {"total_unrealized_pnl": total_unrealized}


def _fetch_account_balances() -> Tuple[Dict[str, Optional[float]], Optional[str]]:
    balances: Dict[str, Optional[float]] = {}
    try:
        records = search_accounts(only_active_accounts=True)
    except Exception as exc:
        logger.warning("Failed to fetch account balances: %s", exc)
        return balances, str(exc)

    by_id = {record.get("id"): record for record in records if record.get("id") is not None}
    by_name = {record.get("name"): record for record in records if record.get("name")}

    for acct_name, acct_id in ACCOUNTS.items():
        record = by_id.get(acct_id) or by_name.get(acct_name)
        if record is None:
            balances[acct_name] = None
            continue
        try:
            balance = record.get("balance")
            balances[acct_name] = float(balance) if balance is not None else None
        except Exception:
            balances[acct_name] = None
    return balances, None


# ─── Payload Builders ────────────────────────────────────────────────────────

def _dashboard_payload(account: str, range_key: str, include_open: bool) -> Dict[str, Any]:
    all_rows, fetch_error = _fetch_ai_trade_feed(account="all", range_key=range_key, include_open=include_open)
    all_open_positions, all_open_totals = _fetch_open_positions_snapshot(account="all")
    account_balances, balance_error = _fetch_account_balances()
    total_balance = (
        sum(balance or 0 for acct_name, balance in account_balances.items() if acct_name != "practice")
        if account_balances
        else None
    )
    rows = all_rows if account == "all" else [row for row in all_rows if row.get("account") == account]
    open_positions = (
        all_open_positions
        if account == "all"
        else [pos for pos in all_open_positions if pos.get("account") == account]
    )
    open_totals = (
        all_open_totals
        if account == "all"
        else {"total_unrealized_pnl": sum(float(p.get("unrealized_pnl") or 0) for p in open_positions)}
    )
    metrics = _compute_metrics(rows, range_key, MOUNTAIN_TZ, open_positions=open_positions)

    account_metrics: List[Dict[str, Any]] = []
    for acct_name in ["all", *ACCOUNTS.keys()]:
        acct_rows = all_rows if acct_name == "all" else [row for row in all_rows if row.get("account") == acct_name]
        acct_open_positions = (
            all_open_positions
            if acct_name == "all"
            else [pos for pos in all_open_positions if pos.get("account") == acct_name]
        )
        acct_metrics = _compute_metrics(acct_rows, range_key, MOUNTAIN_TZ, open_positions=acct_open_positions)
        account_metrics.append(
            {
                "account": "All" if acct_name == "all" else acct_name,
                "metrics": acct_metrics,
            }
        )

    payload: Dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "open_positions": open_positions,
        "open_totals": open_totals,
        "account_metrics": account_metrics,
        "account_balances": account_balances,
        "account_balance_total": total_balance,
        "rows": rows,
    }
    if fetch_error:
        payload["errors"] = fetch_error
    if balance_error:
        if "errors" not in payload or payload["errors"] is None:
            payload["errors"] = {"balance": balance_error}
        elif isinstance(payload["errors"], dict):
            payload["errors"]["balance"] = balance_error
        else:
            payload["errors"] = {"fetch": str(payload["errors"]), "balance": balance_error}
    return payload


# ─── Routes ──────────────────────────────────────────────────────────────────

dashboard_path = "/dashboard"


@dashboard_bp.route(dashboard_path)
def dashboard():
    account = request.args.get("account", "all")
    if account == "all":
        account = "all"
    account = account if account in ACCOUNTS or account == "all" else DEFAULT_ACCOUNT
    range_key = request.args.get("range", "7d")
    include_open = request.args.get("include_open", "true").lower() != "false"
    payload = _dashboard_payload(account, range_key, include_open)
    return render_template(
        "dashboard.html",
        payload=payload,
        accounts=list(ACCOUNTS.keys()),
        default_account=account,
        default_range=range_key,
        default_include_open=include_open,
    )


@dashboard_bp.route(f"{dashboard_path}/data")
def dashboard_data():
    account = request.args.get("account", "all")
    if account != "all" and account not in ACCOUNTS:
        account = DEFAULT_ACCOUNT
    range_key = request.args.get("range", "7d")
    include_open = request.args.get("include_open", "true").lower() != "false"
    payload = _dashboard_payload(account, range_key, include_open)
    return jsonify(payload)

# ─── Daily Reports Dashboard ────────────────────────────────────────────────

REPORTS_DAILY_DIR = (Path(__file__).resolve().parent / "reports" / "daily").resolve()


def _safe_day_dir(day: str) -> Path:
    """
    Prevent path traversal. Only allow directories directly under reports/daily.
    """
    if not day or "/" in day or "\\" in day or ".." in day:
        abort(400, "Invalid day")
    day_dir = (REPORTS_DAILY_DIR / day).resolve()
    if REPORTS_DAILY_DIR not in day_dir.parents:
        abort(400, "Invalid day")
    if not day_dir.exists() or not day_dir.is_dir():
        abort(404, "Report day not found")
    return day_dir


def _list_report_days() -> List[Dict[str, Any]]:
    """
    Returns newest-first list of report day folders with lightweight metadata.
    """
    days: List[Dict[str, Any]] = []
    if not REPORTS_DAILY_DIR.exists():
        return days

    for d in sorted([p for p in REPORTS_DAILY_DIR.iterdir() if p.is_dir()], reverse=True):
        summary_path = d / "summary.json"
        daily_md_path = d / "daily_report.md"
        bundle_path = d / "bundle.zip"

        summary = {}
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except Exception:
                summary = {}

        scripts = summary.get("scripts") or []
        ok_count = sum(1 for s in scripts if s.get("ok"))
        ran_count = sum(1 for s in scripts if s.get("ran"))

        days.append(
            {
                "day": d.name,
                "generated_at": summary.get("generated_at"),
                "session_start_local": summary.get("session_start_local"),
                "session_start_utc": summary.get("session_start_utc"),
                "report_tz": summary.get("report_tz"),
                "script_ok_count": ok_count,
                "script_ran_count": ran_count,
                "bundle_exists": bundle_path.exists(),
                "daily_md_exists": daily_md_path.exists(),
                "has_charts": (d / "charts").exists(),
            }
        )

    return days


def _read_text_file(path: Path, max_chars: int = 80_000) -> str:
    """
    Read a text file safely, truncate to avoid giant pages.
    """
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
        if len(txt) > max_chars:
            return txt[:max_chars] + "\n\n…(truncated)…\n"
        return txt
    except Exception as exc:
        return f"(unable to read {path.name}: {exc})"


@dashboard_bp.route(f"{dashboard_path}/reports")
def dashboard_reports():
    """
    List page of daily reports.
    """
    days = _list_report_days()
    selected = request.args.get("day") or (days[0]["day"] if days else None)
    return render_template(
        "reports.html",
        days=days,
        selected_day=selected,
    )


@dashboard_bp.route(f"{dashboard_path}/reports/<day>")
def dashboard_report_detail(day: str):
    """
    Detail page for a single report day.
    """
    day_dir = _safe_day_dir(day)

    summary_path = day_dir / "summary.json"
    daily_md_path = day_dir / "daily_report.md"

    summary = {}
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summary = {"error": "Failed to parse summary.json"}

    # Load common outputs if they exist
    files_to_show = [
        ("session_report.txt", "Session Report"),
        ("tod_analysis.txt", "Time-of-Day Analysis"),
        ("performance_attribution.txt", "Performance Attribution"),
        ("equity_drawdown_report.txt", "Equity / Drawdown"),
        ("data_quality_report.txt", "Data Quality"),
    ]
    outputs = []
    for fname, label in files_to_show:
        p = day_dir / fname
        if p.exists():
            outputs.append(
                {
                    "filename": fname,
                    "label": label,
                    "content": _read_text_file(p),
                }
            )

    daily_md = _read_text_file(daily_md_path) if daily_md_path.exists() else None

    # chart filenames (served via /dashboard/reports/<day>/files/...)
    charts = []
    chart_dir = day_dir / "charts"
    for tf in ("5m", "15m", "30m"):
        img = chart_dir / f"{tf}.jpg"
        if img.exists():
            charts.append(
                {
                    "tf": tf,
                    "url": f"{dashboard_path}/reports/{day}/files/charts/{tf}.jpg",
                }
            )

    return render_template(
        "report_detail.html",
        day=day,
        summary=summary,
        outputs=outputs,
        daily_md=daily_md,
        charts=charts,
        bundle_url=f"{dashboard_path}/reports/{day}/files/bundle.zip" if (day_dir / "bundle.zip").exists() else None,
        summary_url=f"{dashboard_path}/reports/{day}/files/summary.json" if (day_dir / "summary.json").exists() else None,
    )


@dashboard_bp.route(f"{dashboard_path}/reports/<day>/files/<path:filename>")
def dashboard_report_file(day: str, filename: str):
    """
    Serve files from a report day directory safely (bundle.zip, summary.json, charts/*.jpg, *.txt, *.md).
    """
    day_dir = _safe_day_dir(day)

    # Basic allowlist to reduce risk of serving secrets:
    allowed_ext = (".zip", ".json", ".txt", ".md", ".jpg", ".jpeg", ".png")
    if not any(filename.lower().endswith(ext) for ext in allowed_ext):
        abort(403, "File type not allowed")

    # Prevent traversal inside the day dir
    if ".." in filename or filename.startswith("/") or filename.startswith("\\"):
        abort(400, "Invalid filename")

    # send_from_directory handles safe joining internally for Flask, but we still validated above
    return send_from_directory(day_dir, filename, as_attachment=filename.endswith(".zip"))
