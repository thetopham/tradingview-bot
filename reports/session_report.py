#!/usr/bin/env python3
"""
reports/session_report.py

Daily session report that mirrors the dashboard snapshot:

- Account Performance Snapshot (per account + All)
- Trading-day window stats (4pm MT -> 2pm MT)
- PnL by named sessions (Asia/London/NY Open/etc.)
- Uses /dashboard/data and supports Basic Auth via DASHBOARD_PASSWORD

This replaces the older version that called .json() on an Unauthorized response.
Dashboard auth is enforced when DASHBOARD_PASSWORD is set. :contentReference[oaicite:4]{index=4}
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import pandas as pd
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None


# ----------------------------
# Env / config
# ----------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # repo root
ENV_PATH = PROJECT_ROOT / ".env"

if load_dotenv and ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)

TV_PORT = int(os.getenv("TV_PORT", "5000"))
BASE_URL = f"http://localhost:{TV_PORT}"
DASHBOARD_PASSWORD = (os.getenv("DASHBOARD_PASSWORD") or "").strip()

TZ_LOCAL = ZoneInfo(os.getenv("REPORT_TZ", "America/Denver"))  # MT
OPEN_T = time(16, 0)   # 4pm MT session start
CLOSE_T = time(14, 0)  # 2pm MT session end


# Keep your same buckets (in MT). :contentReference[oaicite:5]{index=5}
SESSIONS: List[Tuple[str, time, time]] = [
    ("Asia",         time(16, 0), time(21, 0)),  # 4pm–9pm
    ("Late US",      time(21, 0), time( 1, 0)),  # 9pm–1am
    ("London",       time( 1, 0), time( 6, 0)),  # 1am–6am
    ("Pre-NY",       time( 6, 0), time( 7,30)),  # 6am–7:30am
    ("NY Open",      time( 7,30), time(10, 0)),  # 7:30am–10am
    ("NY Midday",    time(10, 0), time(12, 0)),  # 10am–12pm
    ("NY Afternoon", time(12, 0), time(14, 0)),  # 12pm–2pm
]


# ----------------------------
# HTTP helpers
# ----------------------------

def _dashboard_auth() -> Optional[Tuple[str, str]]:
    # Dashboard only checks password; username can be anything. :contentReference[oaicite:6]{index=6}
    return ("bot", DASHBOARD_PASSWORD) if DASHBOARD_PASSWORD else None


def fetch_dashboard_payload(
    *,
    account: str = "all",
    range_key: str = "7d",
    include_open: bool = False,
    timeout: int = 25,
) -> Dict[str, Any]:
    auth = _dashboard_auth()
    resp = requests.get(
        f"{BASE_URL}/dashboard/data",
        params={
            "account": account,
            "range": range_key,
            "include_open": "true" if include_open else "false",
        },
        auth=auth,
        timeout=timeout,
    )

    if resp.status_code == 401:
        raise SystemExit(
            "Unauthorized (401) from /dashboard/data. "
            "Set DASHBOARD_PASSWORD in .env and ensure reports send Basic Auth."
        )

    resp.raise_for_status()

    ctype = (resp.headers.get("content-type") or "").lower()
    if "application/json" not in ctype:
        body = (resp.text or "")[:300]
        raise SystemExit(f"Expected JSON from dashboard, got content-type={ctype}. Body={body!r}")

    return resp.json() or {}


# ----------------------------
# Time window helpers
# ----------------------------

def trading_day_window(now_local: datetime) -> Tuple[datetime, datetime]:
    """
    Most recent trading session: 4:00pm -> next day 2:00pm MT.
    End is capped at now unless we're in 2–4pm downtime (session already ended).
    """
    now_t = now_local.timetz().replace(tzinfo=None)

    # Determine session start (most recent 4pm boundary)
    if now_t >= OPEN_T:
        start = now_local.replace(hour=OPEN_T.hour, minute=0, second=0, microsecond=0)
        session_close = (start + timedelta(days=1)).replace(hour=CLOSE_T.hour, minute=0, second=0, microsecond=0)
    else:
        start = (now_local - timedelta(days=1)).replace(hour=OPEN_T.hour, minute=0, second=0, microsecond=0)
        session_close = now_local.replace(hour=CLOSE_T.hour, minute=0, second=0, microsecond=0)

    # If between 2pm and 4pm, cap end at 2pm (session is done)
    if CLOSE_T <= now_t < OPEN_T:
        end = session_close
    else:
        end = min(now_local, session_close)

    return start, end


def in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return (t >= start) or (t < end)


def label_session(dt_local: pd.Timestamp) -> str:
    t = dt_local.time()
    for name, start, end in SESSIONS:
        if in_window(t, start, end):
            return name
    return "Other"


# ----------------------------
# Formatting helpers
# ----------------------------

def _fmt_money(x: Any) -> str:
    try:
        v = float(x)
    except Exception:
        return "-"
    return f"${v:,.2f}"


def _fmt_pct(x: Any) -> str:
    try:
        v = float(x)
    except Exception:
        return "-"
    return f"{v*100:.1f}%"


def _fmt_float(x: Any, places: int = 2) -> str:
    try:
        v = float(x)
    except Exception:
        return "-"
    return f"{v:.{places}f}"


def _profit_factor_from_pnls(pnls: pd.Series) -> Optional[float]:
    wins = pnls[pnls > 0].sum()
    losses = -pnls[pnls < 0].sum()
    if losses == 0:
        return None
    return float(wins / losses)


# ----------------------------
# Snapshot table (mirrors UI)
# ----------------------------

def render_account_snapshot(account_metrics: List[Dict[str, Any]]) -> str:
    """
    account_metrics comes from dashboard payload:
      [{ "account": "All"|"alpha"|..., "metrics": {...}}]
    UI columns are in dashboard.html. :contentReference[oaicite:7]{index=7}
    """
    cols = [
        "Account",
        "Open PnL",
        "Open Details",
        "Today Net",
        "Today Gross",
        "Fees",
        "Trades",
        "Win Rate",
        "Avg Trade",
        "Profit Factor",
        "7D Net",
        "7D Win",
    ]

    rows = []
    for item in account_metrics:
        acct = str(item.get("account") or "")
        m = item.get("metrics") or {}
        openp = m.get("open_positions") or {}
        today = m.get("today") or {}
        week = m.get("seven_day") or {}

        open_details_parts = []
        if openp.get("side"):
            open_details_parts.append(str(openp.get("side")))
        if openp.get("size"):
            open_details_parts.append(f"size {int(openp.get('size') or 0)}")
        if openp.get("duration_minutes"):
            open_details_parts.append(f"{_fmt_float(openp.get('duration_minutes'), 1)} min")
        open_details = " • ".join(open_details_parts) if open_details_parts else "No open positions"

        rows.append([
            acct,
            _fmt_money(openp.get("unrealized_pnl")),
            open_details,
            _fmt_money(today.get("net_pnl")),
            _fmt_money(today.get("gross_pnl")),
            _fmt_money(today.get("fees")),
            str(int(today.get("trade_count") or 0)),
            _fmt_pct(today.get("win_rate")),
            _fmt_money(today.get("avg_trade")),
            _fmt_float(today.get("profit_factor"), 2),
            _fmt_money(week.get("net_pnl")),
            _fmt_pct(week.get("win_rate")),
        ])

    # column widths
    widths = [len(c) for c in cols]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(str(v)))

    def _line(parts):
        return " | ".join(str(p).ljust(widths[i]) for i, p in enumerate(parts))

    out = []
    out.append(_line(cols))
    out.append("-+-".join("-" * w for w in widths))
    for r in rows:
        out.append(_line(r))
    return "\n".join(out)


# ----------------------------
# Main report
# ----------------------------

def main() -> int:
    payload = fetch_dashboard_payload(account="all", range_key="7d", include_open=True)

    updated_at = payload.get("updated_at")
    account_metrics = payload.get("account_metrics") or []
    rows = payload.get("rows") or []

    now_local = datetime.now(TZ_LOCAL)
    start_local, end_local = trading_day_window(now_local)

    print("\nAI TRADE FEED — DAILY SESSION REPORT")
    print(f"Updated: {updated_at}")
    print(f"Window:  {start_local.isoformat()}  →  {end_local.isoformat()}")
    print(f"Source:  {BASE_URL}/dashboard/data (account=all range=7d include_open=true)")
    print("")

    print("=== ACCOUNT PERFORMANCE SNAPSHOT ===")
    print(render_account_snapshot(account_metrics))
    print("")

    # Build per-trade dataframe for window/session stats
    df = pd.DataFrame(rows)
    if df.empty:
        print("No rows returned by dashboard.")
        return 0

    # Use exit_time (realized moment) else decision_time
    ts = df.get("exit_time", pd.Series([None] * len(df))).fillna(df.get("decision_time"))
    dt_local = pd.to_datetime(ts, errors="coerce", utc=True).dt.tz_convert(TZ_LOCAL)

    df["dt_local"] = dt_local
    df["pnl"] = pd.to_numeric(df.get("pnl"), errors="coerce")
    df["account"] = df.get("account").astype(str)

    # Filter to session window
    df = df[(df["dt_local"] >= start_local) & (df["dt_local"] <= end_local)].copy()
    df = df.dropna(subset=["dt_local"])

    if df.empty:
        print(f"No closed trades in window {start_local} → {end_local}.")
        return 0

    df["session"] = df["dt_local"].apply(label_session)
    df["win"] = df["pnl"] > 0

    # Summary by account + session
    grp = df.groupby(["account", "session"], dropna=False)
    summary = grp["pnl"].agg(trades="count", net_pnl="sum", avg="mean").reset_index()
    summary["win_rate"] = grp["win"].mean().values
    summary["pf"] = grp["pnl"].apply(_profit_factor_from_pnls).values

    session_order = [s[0] for s in SESSIONS] + ["Other"]
    summary["session"] = pd.Categorical(summary["session"], categories=session_order, ordered=True)
    summary = summary.sort_values(["account", "session"])

    print("=== PNL BY SESSION (per account) ===")
    # nice formatting
    summary_out = summary.copy()
    summary_out["net_pnl"] = summary_out["net_pnl"].map(_fmt_money)
    summary_out["avg"] = summary_out["avg"].map(_fmt_money)
    summary_out["win_rate"] = summary_out["win_rate"].map(_fmt_pct)
    summary_out["pf"] = summary_out["pf"].map(lambda x: _fmt_float(x, 2) if x is not None else "-")
    print(summary_out.to_string(index=False))
    print("")

    # Wide view + cumulative give-back
    net_by_session = (
        summary.pivot_table(index="account", columns="session", values="net_pnl", aggfunc="sum")
        .fillna(0.0)
        .reindex(columns=session_order, fill_value=0.0)
    )
    cum = net_by_session.cumsum(axis=1)

    print("=== NET PNL BY SESSION (wide) ===")
    print(net_by_session.round(2).to_string())
    print("")
    print("=== CUMULATIVE PNL BY SESSION ORDER (give-back view) ===")
    print(cum.round(2).to_string())
    print("")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
