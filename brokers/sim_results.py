"""Durable, idempotent legacy result rows from simulated closed trades.

The v2 ledger remains authoritative. This outbox is a transport boundary so a
failed Supabase delivery never causes a simulated trade to execute twice.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_result_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    generation INTEGER NOT NULL,
    trade_id INTEGER NOT NULL,
    trace_id TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TEXT,
    UNIQUE(account, generation, trade_id)
);
"""


class SimResults:
    def __init__(self, adapter: Any):
        self.adapter = adapter
        with closing(adapter.ledger.connection()) as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        self.enqueue()

    def enqueue(self, account: str | None = None) -> int:
        """Project missing v2 trades into durable legacy-shaped result rows."""
        with closing(self.adapter.ledger.connection()) as conn:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT t.* FROM sim_trade t LEFT JOIN sim_result_outbox o "
                    "ON o.account=t.account AND o.generation=t.generation AND o.trade_id=t.id "
                    "WHERE o.id IS NULL AND (? IS NULL OR t.account=?) ORDER BY t.id",
                    (account, account),
                ).fetchall()
                for row in rows:
                    payload = self._payload(conn, row)
                    conn.execute(
                        "INSERT OR IGNORE INTO sim_result_outbox "
                        "(account,generation,trade_id,trace_id,payload_json) VALUES (?,?,?,?,?)",
                        (row["account"], row["generation"], row["id"], payload["trace_id"],
                         json.dumps(payload, sort_keys=True, separators=(",", ":"))),
                    )
                return len(rows)

    def _payload(self, conn: Any, row: Any) -> dict[str, Any]:
        account = row["account"]
        account_id = self.adapter.account_ids[account]
        entry_event = conn.execute(
            "SELECT payload_json FROM sim_event WHERE account=? AND generation=? "
            "AND event_type='entry_fill' AND bar_ts=? ORDER BY id DESC LIMIT 1",
            (account, row["generation"], row["entry_ts"]),
        ).fetchone()
        entry = json.loads(entry_event["payload_json"]) if entry_event else {}
        source_bar = entry.get("source_bar_ts")
        decision_event = conn.execute(
            "SELECT payload_json FROM sim_event WHERE account=? AND generation=? "
            "AND event_type='decision' AND bar_ts=? ORDER BY id DESC LIMIT 1",
            (account, row["generation"], source_bar),
        ).fetchone() if source_bar else None
        decision = json.loads(decision_event["payload_json"]) if decision_event else {}
        variant_row = conn.execute(
            "SELECT variant_json FROM sim_account WHERE name=?", (account,)
        ).fetchone()
        variant = json.loads(variant_row["variant_json"]) if variant_row else {}
        fills = self.adapter.trade_fills(row, account_id)
        trace_id = f"sim:{account}:{row['generation']}:{row['id']}"
        ai_decision_id = decision.get("decision_id")
        try:
            ai_decision_id = int(ai_decision_id) if ai_decision_id is not None else None
        except (TypeError, ValueError):
            ai_decision_id = None
        entry_dt = datetime.fromisoformat(row["entry_ts"])
        exit_dt = datetime.fromisoformat(row["exit_ts"])
        return {
            "strategy": variant.get("strategy_id", ""),
            "signal": "BUY" if row["direction"] == "long" else "SELL",
            "symbol": fills[0]["contractId"], "account": account,
            "size": int(entry.get("size") or 1),
            "ai_decision_id": ai_decision_id,
            "entry_time": row["entry_ts"], "exit_time": row["exit_ts"],
            "duration_sec": max(0, int((exit_dt - entry_dt).total_seconds())),
            "alert": "", "total_pnl": row["gross_pnl"],
            "fees_total": round(row["gross_pnl"] - row["net_pnl"], 2),
            "net_pnl": row["net_pnl"],
            "entry_price": row["entry_price"], "exit_price": row["exit_price"],
            "entry_price_source": "sim_fill", "exit_price_source": "sim_fill",
            "raw_trades": fills,
            "order_id": json.dumps([fills[0]["orderId"]]),
            "comment": f"broker=sim | trace_id={trace_id}",
            "trade_ids": [fill["id"] for fill in fills],
            "trace_id": trace_id,
            "session_id": None, "prompt_version": decision.get("prompt_version"),
            "exit_ai_decision_id": None, "exit_reason": row["reason"],
            "exit_signal": "FLAT" if row["reason"] == "signal_flat" else None,
            "exit_trigger": row["reason"], "exit_requested_at": None,
        }

    def after(self, after_id: int, limit: int = 100) -> list[dict[str, Any]]:
        if after_id < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid result cursor or limit")
        with closing(self.adapter.ledger.connection()) as conn:
            rows = conn.execute(
                "SELECT id,account,generation,trade_id,trace_id,payload_json,created_at,published_at "
                "FROM sim_result_outbox WHERE id>? ORDER BY id LIMIT ?", (after_id, limit)
            ).fetchall()
        return [{"id": row["id"], "account": row["account"],
                 "generation": row["generation"], "trade_id": row["trade_id"],
                 "trace_id": row["trace_id"], "payload": json.loads(row["payload_json"]),
                 "created_at": row["created_at"], "published_at": row["published_at"]}
                for row in rows]

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self.adapter.ledger.connection()) as conn:
            rows = conn.execute(
                "SELECT id,trace_id,payload_json FROM sim_result_outbox "
                "WHERE published_at IS NULL ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [{"id": row["id"], "trace_id": row["trace_id"],
                 "payload": json.loads(row["payload_json"])} for row in rows]

    def mark_published(self, row_id: int) -> None:
        with closing(self.adapter.ledger.connection()) as conn:
            with conn:
                conn.execute("UPDATE sim_result_outbox SET published_at=CURRENT_TIMESTAMP "
                             "WHERE id=? AND published_at IS NULL", (row_id,))
