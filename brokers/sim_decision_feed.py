"""Durable closed decision candles for the separate simulation scheduler."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3

from tvbot_v2.simulate.brokerless_executor import utc


SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_decision_feed (
    timeframe TEXT NOT NULL,
    bar_ts TEXT NOT NULL,
    bar_json TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(timeframe, bar_ts)
);
"""


def record(db_path: str, timeframe: str, bar: dict) -> None:
    encoded = json.dumps(bar, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.executescript(SCHEMA)
        existing = conn.execute(
            "SELECT bar_json FROM sim_decision_feed WHERE timeframe=? AND bar_ts=?",
            (timeframe, utc(bar["timestamp"]).isoformat()),
        ).fetchone()
        if existing and existing[0] != encoded:
            raise ValueError("conflicting closed decision bar")
        conn.execute("INSERT OR IGNORE INTO sim_decision_feed(timeframe,bar_ts,bar_json) "
                     "VALUES (?,?,?)", (timeframe, utc(bar["timestamp"]).isoformat(), encoded))


def latest_fresh(db_path: str, timeframe: str, now: datetime,
                 *, max_age_seconds: int = 300) -> dict | None:
    minutes = int(timeframe.removesuffix("m"))
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT bar_ts,bar_json FROM sim_decision_feed "
                           "WHERE timeframe=? ORDER BY bar_ts DESC LIMIT 1",
                           (timeframe,)).fetchone()
    if not row:
        return None
    closed = utc(row[0]) + timedelta(minutes=minutes)
    age = now.astimezone(timezone.utc) - closed
    if age < timedelta(0) or age > timedelta(seconds=max_age_seconds):
        return None
    return json.loads(row[1])

