#!/usr/bin/env python3
"""
reports/data_quality_report.py

Mature systems track data integrity:
- % trades with screenshot url
- % trades with entry/exit prices
- % trades with entry reason
- flatten exits with missing exit_reason / exit_ai_decision_id

Uses /dashboard/data rows (range=30d include_open=true).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

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
        params={"account": "all", "range": range_key, "include_open": "true"},
        auth=_auth(),
        timeout=25,
    )
    if resp.status_code == 401:
        raise SystemExit("Unauthorized (401). Check DASHBOARD_PASSWORD.")
    resp.raise_for_status()
    return resp.json() or {}


def pct(n: int, d: int) -> str:
    if d <= 0:
        return "-"
    return f"{(n/d)*100:.1f}%"


def main() -> int:
    payload = fetch_rows("30d")
    df = pd.DataFrame(payload.get("rows") or [])
    if df.empty:
        print("No rows returned.")
        return 0

    df["account"] = df.get("account").astype(str)

    # completeness flags
    df["has_screenshot"] = df.get("screenshot").notna() & (df.get("screenshot").astype(str).str.len() > 5)
    df["has_entry_reason"] = df.get("reason").notna() & (df.get("reason").astype(str).str.strip().str.len() > 0)
    df["has_entry_px"] = pd.to_numeric(df.get("entry_price"), errors="coerce").notna()
    df["has_exit_px"] = pd.to_numeric(df.get("exit_price"), errors="coerce").notna()

    # flatten exits
    df["is_flatten_exit"] = df.get("is_flatten_exit").fillna(False).astype(bool)
    df["has_exit_reason"] = df.get("exit_reason").notna() & (df.get("exit_reason").astype(str).str.strip().str.len() > 0)
    df["has_exit_ai_id"] = df.get("exit_ai_decision_id").notna()

    rows_out = []
    for acct, g in df.groupby("account"):
        total = len(g)
        rows_out.append({
            "account": acct,
            "rows": total,
            "screenshot%": pct(int(g["has_screenshot"].sum()), total),
            "entry_reason%": pct(int(g["has_entry_reason"].sum()), total),
            "entry_px%": pct(int(g["has_entry_px"].sum()), total),
            "exit_px%": pct(int(g["has_exit_px"].sum()), total),
            "flatten_rows": int(g["is_flatten_exit"].sum()),
            "flatten_exit_reason_missing": int((g["is_flatten_exit"] & ~g["has_exit_reason"]).sum()),
            "flatten_exit_ai_id_missing": int((g["is_flatten_exit"] & ~g["has_exit_ai_id"]).sum()),
        })

    out = pd.DataFrame(rows_out).sort_values(["rows"], ascending=False)

    print("\nDATA QUALITY REPORT (Last 30d)\n")
    print(out.to_string(index=False))
    print("")
    print("Tip: flatten_exit_reason_missing > 0 means exits happened but weren’t linked to an exit decision.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
