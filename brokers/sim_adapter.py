"""ProjectX-shaped read views over the persistent v2 simulated broker.

All state changes require a closed-bar envelope. This adapter has no broker
credentials, HTTP order endpoint, or SignalR dependency.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from contextlib import closing
from typing import Any
from uuid import uuid4

from tvbot_v2.simulate.brokerless_executor import SimBroker
from tvbot_v2.simulate.brokerless_executor import utc
from tvbot_v2.simulate.ledger import SimLedger


SIM_CONTRACT = "CON.F.US.MES.SIM"


class SimAdapter:
    def __init__(self, db_path: str | Path, account_ids: dict[str, int]):
        path = Path(db_path)
        if not path.is_file():
            raise ValueError(f"simulated broker database does not exist: {path}")
        self.ledger = SimLedger(path)
        self.broker = SimBroker(self.ledger)
        self.account_ids = account_ids
        self.names_by_id = {value: key for key, value in account_ids.items()}
        from brokers.sim_results import SimResults
        self.results = SimResults(self)

    def name(self, account_id: int) -> str:
        try:
            return self.names_by_id[int(account_id)]
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"unknown simulated account id: {account_id}") from exc

    def status(self, name: str) -> dict[str, Any]:
        if name not in self.account_ids:
            raise ValueError(f"unknown simulated account: {name}")
        return self.ledger.status(name)[0]

    def accounts(self, active_only: bool = True) -> list[dict[str, Any]]:
        rows = []
        for snapshot in self.ledger.status():
            active = snapshot["status"] == "active" and not snapshot["manual_paused"]
            if active_only and not active:
                continue
            rows.append({"id": self.account_ids[snapshot["account"]],
                         "name": snapshot["account"], "balance": snapshot["balance"],
                         "canTrade": active, "broker": "sim"})
        return rows

    def positions(self, account_id: int) -> list[dict[str, Any]]:
        name = self.name(account_id)
        position = self.status(name)["position"]
        if position is None:
            return []
        return [{"accountId": int(account_id), "contractId": SIM_CONTRACT,
                 "type": 1 if position["direction"] == 1 else 2,
                 "size": position["quantity"], "averagePrice": position["entry_price"],
                 "creationTimestamp": position["entry_ts"]}]

    def open_orders(self, account_id: int) -> list[dict[str, Any]]:
        name = self.name(account_id)
        snapshot = self.status(name)
        position = snapshot["position"]
        orders = []
        pending = snapshot["pending"]
        if pending and pending.get("order_id"):
            orders.append({"id": f"SIM-{pending['order_id']}",
                           "accountId": int(account_id), "contractId": SIM_CONTRACT,
                           "type": 2, "side": 0 if pending["signal"] == "BUY" else 1,
                           "size": pending["size"], "status": 1,
                           "creationTimestamp": pending["source_bar_ts"]})
        if position is None:
            return orders
        stem = f"SIM-{name}-{position['entry_ts']}"
        side = 1 if position["direction"] == 1 else 0
        orders.extend([
            {"id": stem + "-SL", "accountId": int(account_id), "contractId": SIM_CONTRACT,
             "type": 4, "side": side, "size": position["quantity"], "status": 1,
             "stopPrice": position["stop_price"]},
            {"id": stem + "-TP", "accountId": int(account_id), "contractId": SIM_CONTRACT,
             "type": 1, "side": side, "size": position["quantity"], "status": 1,
             "limitPrice": position["target_price"]},
        ])
        return orders

    def submit_market_order(self, account_id: int, side: int, size: int,
                            client_order_id: str | None = None,
                            metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        if side not in (0, 1):
            raise ValueError("market order side must be 0 (buy) or 1 (sell)")
        name = self.name(account_id)
        return self.broker.submit_order(name, "BUY" if side == 0 else "SELL", size,
                                        client_order_id or uuid4().hex, metadata=metadata)

    def flatten(self, account_id: int, client_order_id: str | None = None) -> dict[str, Any]:
        name = self.name(account_id)
        return self.broker.submit_order(name, "FLAT", 1,
                                        client_order_id or uuid4().hex)

    def cancel_order(self, account_id: int, order_id: str) -> bool:
        return self.broker.cancel_order(self.name(account_id), order_id)

    def trades(self, account_id: int, since: datetime) -> list[dict[str, Any]]:
        name = self.name(account_id)
        snapshot = self.status(name)
        with closing(self.ledger.connection()) as conn:
            rows = conn.execute(
                "SELECT * FROM sim_trade WHERE account=? AND generation=? ORDER BY id",
                (name, snapshot["generation"])).fetchall()
        fills = []
        with closing(self.ledger.connection()) as conn:
            for row in rows:
                if datetime.fromisoformat(row["exit_ts"]) < since:
                    continue
                entry_event = conn.execute(
                    "SELECT payload_json FROM sim_event WHERE account=? AND generation=? "
                    "AND event_type='entry_fill' AND bar_ts=? ORDER BY id DESC LIMIT 1",
                    (name, row["generation"], row["entry_ts"])).fetchone()
                entry_order_id = (json.loads(entry_event["payload_json"]).get("order_id")
                                  if entry_event else None)
                fills.extend(self.trade_fills(row, account_id, entry_order_id))
        return fills

    @staticmethod
    def trade_fills(row: Any, account_id: int,
                    entry_order_id: str | None = None) -> list[dict[str, Any]]:
        entry_side = 0 if row["direction"] == "long" else 1
        fee = round((row["gross_pnl"] - row["net_pnl"]) / 2, 2)
        return [{"id": f"SIM-{phase}-{row['id']}", "accountId": int(account_id),
                 "contractId": SIM_CONTRACT,
                 "orderId": entry_order_id if phase == "ENTRY" and entry_order_id
                            else f"SIM-{phase}-{row['id']}",
                 "side": side, "size": row["quantity"], "price": price,
                 "profitAndLoss": pnl, "fees": fee,
                 "creationTimestamp": timestamp, "voided": False,
                 "raw_source": "sim_broker"}
                for phase, side, price, timestamp, pnl in (
                    ("ENTRY", entry_side, row["entry_price"], row["entry_ts"], None),
                    ("EXIT", 1 - entry_side, row["exit_price"], row["exit_ts"], row["gross_pnl"]),
                )]

    def account_context(self, name: str) -> dict[str, Any]:
        snapshot = self.status(name)
        p = snapshot["position"]
        return {"current_position": {
                    "has_position": p is not None,
                    "side": ("LONG" if p["direction"] == 1 else "SHORT") if p else None,
                    "size": p["quantity"] if p else 0,
                    "average_price": p["entry_price"] if p else None,
                    "unrealized_pnl": round(snapshot["equity"] - snapshot["balance"], 2),
                    "duration_minutes": None,
                },
                "account_metrics": {"account_balance": snapshot["balance"],
                                    "daily_pnl": snapshot["current_day_pnl"],
                                    "win_rate": snapshot["win_rate"],
                                    "consecutive_losses": snapshot["consecutive_losses"]},
                "topstep": {"equity_peak_usd": snapshot["eod_balance_peak"],
                            "trailing_dd_used_usd": snapshot["drawdown_used"],
                            "trailing_dd_remaining_usd": snapshot["mll_remaining"]},
                "warnings": [snapshot["status"]] if snapshot["status"] != "active" else []}

    def latest_price(self, max_age_seconds: int) -> float | None:
        with closing(self.ledger.connection()) as conn:
            rows = conn.execute("SELECT last_bar_ts,last_bar_close,variant_json FROM sim_account "
                                "WHERE last_bar_ts IS NOT NULL").fetchall()
        from tvbot_v2.simulate.portfolio import SimVariant
        values = []
        for row in rows:
            variant = SimVariant.from_json(row["variant_json"])
            closed = datetime.fromisoformat(row["last_bar_ts"]) + timedelta(minutes=variant.execution_minutes)
            if datetime.now(timezone.utc) - closed <= timedelta(seconds=max_age_seconds):
                values.append((closed, row["last_bar_close"]))
        return max(values)[1] if values else None

    def process_closed_bar(self, name: str, bar: dict[str, Any],
                           decision: dict[str, Any]) -> dict[str, Any]:
        snapshot = self.status(name)
        candidate = dict(decision)
        candidate.setdefault("account", name)
        candidate.setdefault("timeframe", snapshot["timeframe"])
        candidate.setdefault("bar_ts", bar["timestamp"])
        result = self.broker.process_envelope({"account": name, "bar": bar, "decision": candidate})
        self.results.enqueue(name)
        return result

    def process_decision(self, name: str, bar_ts: str,
                         decision: dict[str, Any]) -> dict[str, Any]:
        snapshot = self.status(name)
        candidate = dict(decision)
        candidate.setdefault("account", name)
        candidate.setdefault("timeframe", snapshot["timeframe"])
        candidate.setdefault("bar_ts", bar_ts)
        return self.broker.submit_decision(name, bar_ts, candidate)

    def process_execution_bar(self, name: str, bar: dict[str, Any]) -> dict[str, Any]:
        result = self.broker.process_envelope({"account": name, "bar": bar})
        self.results.enqueue(name)
        return result

    def processed_decision(self, name: str, bar_ts: str) -> dict[str, Any] | None:
        snapshot = self.status(name)
        with closing(self.ledger.connection()) as conn:
            row = conn.execute("SELECT snapshot_json FROM sim_decision WHERE account=? AND generation=? AND bar_ts=?",
                               (name, snapshot["generation"], utc(bar_ts).isoformat())).fetchone()
        return json.loads(row["snapshot_json"]) if row else None

    def processed_snapshot(self, name: str, bar_ts: str) -> dict[str, Any] | None:
        snapshot = self.status(name)
        with closing(self.ledger.connection()) as conn:
            row = conn.execute("SELECT snapshot_json FROM sim_bar WHERE account=? AND generation=? AND bar_ts=?",
                               (name, snapshot["generation"], utc(bar_ts).isoformat())).fetchone()
        return json.loads(row["snapshot_json"]) if row else None

    def events_after(self, after_id: int, limit: int = 100) -> list[dict[str, Any]]:
        if after_id < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid event cursor or limit")
        with closing(self.ledger.connection()) as conn:
            rows = conn.execute("SELECT id,account,generation,bar_ts,event_type,payload_json,created_at "
                                "FROM sim_event WHERE id>? ORDER BY id LIMIT ?",
                                (after_id, limit)).fetchall()
        return [{"id": row["id"], "account": row["account"],
                 "generation": row["generation"], "bar_ts": row["bar_ts"],
                 "event_type": row["event_type"], "payload": json.loads(row["payload_json"]),
                 "created_at": row["created_at"]} for row in rows]

    def broker_events_after(self, after_id: int, limit: int = 100) -> tuple[list[dict[str, Any]], int]:
        """Render local ledger events as the old SignalR account/order/position/trade shapes."""
        messages = []
        source_events = self.events_after(after_id, limit)
        for event in source_events:
            account_id = self.account_ids[event["account"]]
            payload = event["payload"]
            kind = event["event_type"]
            common = {"accountId": account_id, "contractId": SIM_CONTRACT,
                      "creationTimestamp": event["bar_ts"] or event["created_at"]}
            data = []
            if kind == "order_submitted":
                data.append(("GatewayUserOrder", {**common, "id": payload["order_id"],
                           "status": 1, "side": 0 if payload["signal"] == "BUY" else 1,
                           "size": payload["size"]}))
            elif kind == "order_cancelled":
                data.append(("GatewayUserOrder", {**common, "id": payload["order_id"],
                           "status": 3}))
            elif kind == "entry_fill":
                order_id = payload.get("order_id") or f"SIM-ENTRY-{event['id']}"
                data.extend([
                    ("GatewayUserOrder", {**common, "id": order_id, "status": 2,
                     "side": 0 if payload["signal"] == "BUY" else 1,
                     "size": payload["quantity"], "averageFillPrice": payload["price"]}),
                    ("GatewayUserPosition", {**common,
                     "type": 1 if payload["signal"] == "BUY" else 2,
                     "size": payload["quantity"], "averagePrice": payload["price"]}),
                ])
            elif kind == "trade_closed":
                data.extend([
                    ("GatewayUserPosition", {**common, "size": 0,
                     "profitAndLoss": payload["gross_pnl"]}),
                    ("GatewayUserTrade", {**common, "price": payload["exit_price"],
                     "size": payload["quantity"], "profitAndLoss": payload["gross_pnl"],
                     "netPnl": payload["net_pnl"]}),
                ])
            elif kind == "status_changed":
                data.append(("GatewayUserAccount", {"id": account_id,
                            "canTrade": payload["to"] == "active"}))
            for name, item in data:
                messages.append({"id": event["id"], "account": event["account"],
                                 "event": name, "data": item})
        return messages, source_events[-1]["id"] if source_events else after_id
