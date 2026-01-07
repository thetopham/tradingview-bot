#!/usr/bin/env python3
"""
reports/performance_attribution.py

Performance attribution like mature systems:
- by account x signal (BUY/SELL)
- by account x size
- by account x strategy

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


def _pf(pnls: pd.Series):
    wins = pnls[pnls > 0].sum()
    losses = -pnls[pnls < 0].sum()
    if losses == 0:
        return None
    return float(wins / losses)


def _summ(df: pd.DataFrame, keys):
    g = df.groupby(keys)["pnl"]
    out = g.agg(trades="count", net="sum", avg="mean").reset_index()
    out["win_rate"] = df.groupby(keys)["win"].mean().values
    out["profit_factor"] = g.apply(_pf).values
    out = out.sort_values(["net"], ascending=False)
    return out


def main() -> int:
    payload = fetch_rows("30d")
    df = pd.DataFrame(payload.get("rows") or [])
    if df.empty:
        print("No rows returned.")
        return 0

    df["pnl"] = pd.to_numeric(df.get("pnl"), errors="coerce")
    df = df.dropna(subset=["pnl"]).copy()
    df["account"] = df.get("account").astype(str)
    df["signal"] = df.get("signal").astype(str).str.upper()
    df["strategy"] = df.get("strategy").astype(str)
    df["size"] = pd.to_numeric(df.get("size"), errors="coerce")
    df["win"] = df["pnl"] > 0

    print("\nPERFORMANCE ATTRIBUTION (Last 30d)\n")

    print("=== By account x signal ===")
    print(_summ(df, ["account", "signal"]).to_string(index=False))
    print("\n=== By account x size ===")
    print(_summ(df.dropna(subset=["size"]), ["account", "size"]).to_string(index=False))
    print("\n=== By account x strategy ===")
    print(_summ(df, ["account", "strategy"]).to_string(index=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
