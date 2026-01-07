#!/usr/bin/env python3
"""
reports/equity_drawdown_report.py

Mature system staples:
- equity curve (cumulative PnL) per account
- max drawdown
- max win/loss streak
- expectancy (avg pnl, win rate)

Uses /dashboard/data rows (range=30d include_open=false).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import requests
import pandas as pd

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


def _auth():
    return ("bot", DASHBOARD_PASSWORD) if DASHBOARD_PASSWORD else None


def fetch_rows(range_key: str = "30d") -> Dict[str, Any]:
    resp = requests.get(
        f"{BASE_URL}/dashboard/data",
        params={"account": "all", "range": range_key, "include_open": "false"},
        auth=_auth(),
        timeout=25,
    )
    if resp.status_code == 401:
        raise SystemExit("Unauthorized (401). Check DASHBOARD_PASSWORD.")
    resp.raise_for_status()
    return resp.json() or {}


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = equity - peak
    return float(dd.min()) if len(dd) else 0.0


def max_streak(outcomes: pd.Series, kind: str) -> int:
    # outcomes: "W"/"L"
    best = cur = 0
    for o in outcomes:
        if o == kind:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return int(best)


def main() -> int:
    payload = fetch_rows("30d")
    df = pd.DataFrame(payload.get("rows") or [])
    if df.empty:
        print("No rows returned.")
        return 0

    # realized timestamp
    ts = df.get("exit_time", pd.Series([None] * len(df))).fillna(df.get("decision_time"))
    df["ts"] = pd.to_datetime(ts, errors="coerce", utc=True)

    df["pnl"] = pd.to_numeric(df.get("pnl"), errors="coerce")
    df = df.dropna(subset=["ts", "pnl"]).copy()

    df["account"] = df.get("account").astype(str)
    df = df.sort_values(["account", "ts"])

    rows_out = []
    for acct, g in df.groupby("account"):
        pnl = g["pnl"]
        equity = pnl.cumsum()
        dd = max_drawdown(equity)
        trades = int(len(g))
        win_rate = float((pnl > 0).mean()) if trades else 0.0
        avg = float(pnl.mean()) if trades else 0.0

        outcomes = pnl.apply(lambda x: "W" if x > 0 else ("L" if x < 0 else ""))
        outcomes = outcomes[outcomes != ""]
        max_win = max_streak(outcomes, "W")
        max_loss = max_streak(outcomes, "L")

        rows_out.append({
            "account": acct,
            "trades": trades,
            "net": float(pnl.sum()),
            "avg_trade": avg,
            "win_rate": win_rate,
            "max_drawdown": dd,      # negative number
            "max_win_streak": max_win,
            "max_loss_streak": max_loss,
        })

    out = pd.DataFrame(rows_out).sort_values("net", ascending=False)

    def money(x): return f"${float(x):,.2f}"
    def pct(x): return f"{float(x)*100:.1f}%"

    out_fmt = out.copy()
    out_fmt["net"] = out_fmt["net"].map(money)
    out_fmt["avg_trade"] = out_fmt["avg_trade"].map(money)
    out_fmt["win_rate"] = out_fmt["win_rate"].map(pct)
    out_fmt["max_drawdown"] = out_fmt["max_drawdown"].map(money)

    print("\nEQUITY / DRAWDOWN REPORT (Last 30d)\n")
    print(out_fmt.to_string(index=False))
    print("")
    print("Note: max_drawdown is the worst peak-to-trough dip on cumulative PnL (more negative = worse).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
