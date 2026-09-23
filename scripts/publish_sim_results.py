"""Publish the simulated trade outbox to the existing trade_results table.

Use a separate process from the webhook. If delivery fails, the durable row
remains pending and a later run checks trace_id before retrying the insert.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import sys

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brokers.sim_adapter import SimAdapter


def publish_pending(adapter: SimAdapter, url: str, key: str,
                    *, limit: int = 100, session: requests.Session | None = None) -> int:
    if not url or not key:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY are required to publish")
    if not 1 <= limit <= 500:
        raise ValueError("limit must be from 1 to 500")
    client = session or requests.Session()
    endpoint = url.rstrip("/") + "/rest/v1/trade_results"
    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json"}
    count = 0
    for row in adapter.results.pending(limit):
        existing = client.get(endpoint, params={"select": "id", "trace_id": f"eq.{row['trace_id']}",
                                                "limit": "1"}, headers=headers, timeout=(3.05, 10))
        existing.raise_for_status()
        if not existing.json():
            response = client.post(endpoint, json=row["payload"],
                                   headers={**headers, "Prefer": "return=minimal"},
                                   timeout=(3.05, 10))
            response.raise_for_status()
        adapter.results.mark_published(row["id"])
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Count pending results without network calls")
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    load_dotenv()
    path = os.getenv("SIM_BROKER_DB")
    if not path or not Path(path).is_file():
        parser.error("SIM_BROKER_DB must point to an initialized v2 database")
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        accounts = {name: 900000 + rowid for rowid, name in
                    conn.execute("SELECT rowid,name FROM sim_account ORDER BY rowid")}
    adapter = SimAdapter(path, accounts)
    if args.dry_run:
        print(f"pending={len(adapter.results.pending(args.limit))}")
        return 0
    try:
        count = publish_pending(adapter, os.getenv("SUPABASE_URL", ""),
                                os.getenv("SUPABASE_KEY", ""), limit=args.limit)
    except (ValueError, requests.RequestException) as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 1
    print(f"published={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
