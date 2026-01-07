#!/usr/bin/env python3
"""
reports/tod_analysis.py

Time-of-day performance (hourly) for the most recent trading session window (4pm→2pm MT).

- Pulls /dashboard/data (range=7d include_open=false)
- Uses Basic Auth if DASHBOARD_PASSWORD is set
- Outputs per-account hourly table + best/worst hour summary
"""

from __future__ import annotations

import os
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
import pandas as pd
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

if load_dotenv and ENV_PATH.exists():
    load_dotenv(dotenv_path=ENV_PATH)

TV_PORT = int(os.getenv("TV_PORT", "5000"))
BASE_URL = f"http://localhost:{TV_PORT}"
DASHBOARD_PASSWORD = (os.getenv("DASHBOARD_PASSWORD") or "").strip()

TZ = ZoneInfo(os.getenv("REPORT_TZ", "America/Denver"))
OPEN_T = time(16, 0)   # 4pm MT
CLOSE_T = time(14, 0)  # 2pm MT


def _auth():
    return ("bot", DASHBOARD_PASSWORD) if DASHBOARD_PASSWORD else None


def trading_day_window(now_local: datetime) -> Tuple[datetime, datetime]:
    now_t = now_local.timetz().replace(tzinfo=None)

    if now_t >= OPEN_T:
        start = now_local.replace(hour=OPEN_T.hour, minute=0, second=0, microsecond=0)
        session_close = (start + timedelta(days=1)).replace(hour=CLOSE_T.hour, minute=0, second=0, microsecond=0)
    else:
        start = (now_local - timedelta(days=1)).replace(hour=OPEN_T.hour, minute=0, second=0, microsecond=0)
        session_close = now_local.replace(hour=CLOSE_T.hour, minute=0, second=0, microsecond=0)

    if CLOSE_T <= now_t < OPEN_T:
        end = session_close
    else:
        end = min(now_local, session_close)

    return start, end


def fetch_rows() -> Dict[str, Any]:
    resp = requests.get(
        f"{BASE_URL}/dashboard/data",
        params={"account": "all", "range": "7d", "include_open": "false"},
        auth=_auth(),
        timeout=25,
    )
    if resp.status_code == 401:
        raise SystemExit("Unauthorized (401) from /dashboard/data. Check DASHBOARD_PASSWORD.")
    resp.raise_for_status()
    return resp.json() or {}


def main() -> int:
    now_local = datetime.now(TZ)
    start_local, end_local = trading_day_window(now_local)

    payload = fetch_rows()
    rows = payload.get("rows") or []

    df = pd.DataFrame(rows)
    if df.empty:
        print("No rows returned.")
        return 0

    # Use exit_time (realized PnL moment) for hour attribution; fallback to decision_time
    ts = df.get("exit_time", pd.Series([None] * len(df))).fillna(df.get("decision_time"))
    dt = pd.to_datetime(ts, errors="coerce", utc=True).dt.tz_convert(TZ)

    df["dt_local"] = dt
    df["pnl"] = pd.to_numeric(df.get("pnl"), errors="coerce")
    df["account"] = df.get("account").astype(str)

    # Filter to the trading-day window
    df = df[(df["dt_local"] >= start_local) & (df["dt_local"] <= end_local)].copy()
    df = df.dropna(subset=["dt_local", "pnl"])

    if df.empty:
        print(f"No rows considered between {start_local} and {end_local}.")
        return 0

    df["hour"] = df["dt_local"].dt.hour
    df["win"] = df["pnl"] > 0

    hourly = (
        df.groupby(["account", "hour"])["pnl"]
        .agg(trades="count", net="sum", avg="mean")
        .reset_index()
        .sort_values(["account", "hour"])
    )
    hourly["win_rate"] = (
        df.groupby(["account", "hour"])["win"].mean().reset_index(drop=True)
    )

    def _fmt_money(x):
        return f"{float(x):,.2f}"

    def _fmt_pct(x):
        return f"{float(x)*100:.1f}%"

    print("\nTIME-OF-DAY PERFORMANCE (HOURLY)")
    print(f"Window: {start_local}  →  {end_local}\n")

    out = hourly.copy()
    out["net"] = out["net"].map(_fmt_money)
    out["avg"] = out["avg"].map(_fmt_money)
    out["win_rate"] = out["win_rate"].map(_fmt_pct)
    print(out.to_string(index=False))
    print("")

    # Best/worst hour per account
    best = hourly.sort_values(["account", "net"], ascending=[True, False]).groupby("account").head(1)
    worst = hourly.sort_values(["account", "net"], ascending=[True, True]).groupby("account").head(1)

    print("BEST HOUR (per account)")
    print(best[["account", "hour", "trades", "net", "avg"]].to_string(index=False))
    print("")
    print("WORST HOUR (per account)")
    print(worst[["account", "hour", "trades", "net", "avg"]].to_string(index=False))
    print("")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
