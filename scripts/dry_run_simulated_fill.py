#!/usr/bin/env python3
"""Dry-run a simulated broker fill through the legacy trade-results logger.

This script is intentionally paper/sim only:
- no ProjectX/Topstep auth
- no real order placement
- no SignalR import or connection
- no Supabase network write by default

It monkey-patches the narrow dependencies used by api.log_trade_results_to_supabase:
- api.post('/api/Trade/search', ...) returns synthetic ProjectX-shaped trades
- api.get_supabase_client() returns an empty fake client for idempotency lookups
- api.session.post(...) captures the would-be trade_results insert payload

The goal is to prove a SimBroker/PaperBroker fill can preserve the legacy
trade_results payload shape while bypassing dead broker streaming dependencies.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
DRY_SUPABASE_URL = "http://supabase-dry-run.invalid"
DRY_SUPABASE_KEY = "dry-run-key"
DEFAULT_ACCOUNT_NAME = "paper"
DEFAULT_ACCOUNT_ID = 999001
DEFAULT_CONTRACT_ID = "CON.F.US.MES.SIM"
DEFAULT_ENTRY_ORDER_ID = "SIM-ENTRY-0001"
DEFAULT_EXIT_ORDER_ID = "SIM-EXIT-0001"


def _safe_import_api():
    """Import api.py after installing safe defaults needed by config.load_config()."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # config.load_config() requires at least one ACCOUNT_* env var. setdefault keeps
    # an operator's real .env intact but guarantees this script can run in isolation.
    os.environ.setdefault("ACCOUNT_PAPER", str(DEFAULT_ACCOUNT_ID))
    os.environ.setdefault("PROJECTX_BASE_URL", "http://projectx-dry-run.invalid")
    os.environ.setdefault("PROJECTX_USERNAME", "dry-run-user")
    os.environ.setdefault("PROJECTX_API_KEY", "dry-run-api-key")
    os.environ.setdefault("SUPABASE_URL", DRY_SUPABASE_URL)
    os.environ.setdefault("SUPABASE_KEY", DRY_SUPABASE_KEY)

    # api.py imports the Supabase SDK at module import time. The dry-run path
    # monkey-patches get_supabase_client() before use, so provide a tiny import
    # stub when the SDK is not installed in a lightweight dev/test environment.
    if "supabase" not in sys.modules:
        try:
            __import__("supabase")
        except ModuleNotFoundError:
            supabase_stub = types.ModuleType("supabase")
            supabase_stub.create_client = lambda *args, **kwargs: None
            sys.modules["supabase"] = supabase_stub

    import api  # noqa: PLC0415

    return api


class FakeResponse:
    def __init__(self, status_code: int = 201, data: Optional[dict] = None, text: str = "dry-run accepted"):
        self.status_code = status_code
        self._data = data if data is not None else {"success": True, "dry_run": True}
        self.text = text

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"FakeResponse HTTP {self.status_code}: {self.text}")


class FakeSupabaseQuery:
    """Chainable no-network subset used by log_trade_results_to_supabase()."""

    def __init__(self, client: "FakeSupabaseClient", table_name: str):
        self.client = client
        self.table_name = table_name
        self.operation = "select"
        self.payload = None
        self.filters: List[tuple] = []

    def select(self, *args, **kwargs):
        self.operation = "select"
        return self

    def eq(self, *args, **kwargs):
        self.filters.append(("eq", args, kwargs))
        return self

    def gte(self, *args, **kwargs):
        self.filters.append(("gte", args, kwargs))
        return self

    def lte(self, *args, **kwargs):
        self.filters.append(("lte", args, kwargs))
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def update(self, payload: dict):
        self.operation = "update"
        self.payload = payload
        return self

    def execute(self):
        if self.operation == "update":
            self.client.updates.append({"table": self.table_name, "payload": self.payload, "filters": self.filters})
        # Empty data forces log_trade_results_to_supabase down the insert path,
        # letting us capture the legacy trade_results payload without touching Supabase.
        return SimpleNamespace(data=[])


class FakeSupabaseClient:
    def __init__(self):
        self.queries: List[str] = []
        self.updates: List[dict] = []

    def table(self, table_name: str) -> FakeSupabaseQuery:
        self.queries.append(table_name)
        return FakeSupabaseQuery(self, table_name)


@dataclass
class CaptureSession:
    inserts: List[dict] = field(default_factory=list)

    def post(self, url: str, json: Optional[dict] = None, headers: Optional[dict] = None, timeout: Any = None):
        safe_headers = {k: "[REDACTED]" for k in (headers or {}).keys()}
        self.inserts.append({"url": url, "json": json, "headers": safe_headers, "timeout": timeout})
        logging.info("[dry-run] Captured Supabase insert url=%s payload_keys=%s", url, sorted((json or {}).keys()))
        return FakeResponse(status_code=201, text="dry-run captured trade_results insert")


@dataclass
class PatchState:
    api: Any
    original_post: Any
    original_session: Any
    original_get_supabase_client: Any
    original_supabase_url: Any
    original_supabase_key: Any
    original_accounts: Any

    def restore(self) -> None:
        self.api.post = self.original_post
        self.api.session = self.original_session
        self.api.get_supabase_client = self.original_get_supabase_client
        self.api.SUPABASE_URL = self.original_supabase_url
        self.api.SUPABASE_KEY = self.original_supabase_key
        self.api.ACCOUNTS = self.original_accounts


def _build_simulated_projectx_trades(
    *,
    account_id: int,
    contract_id: str,
    entry_order_id: str,
    exit_order_id: str,
    entry_time: datetime,
    exit_time: datetime,
    size: int,
    entry_price: float,
    exit_price: float,
    gross_pnl: float,
    fees_total: float,
) -> List[Dict[str, Any]]:
    """Return ProjectX-shaped trade dictionaries expected by api.log_trade_results_to_supabase."""
    return [
        {
            "id": "SIM-TRADE-ENTRY-0001",
            "accountId": account_id,
            "contractId": contract_id,
            "orderId": entry_order_id,
            "side": 0,  # ProjectX compatibility: 0=BUY
            "size": size,
            "price": entry_price,
            "profitAndLoss": None,
            "fees": fees_total / 2,
            "creationTimestamp": entry_time.astimezone(timezone.utc).isoformat(),
            "voided": False,
            "raw_source": "sim_broker_dry_run",
        },
        {
            "id": "SIM-TRADE-EXIT-0001",
            "accountId": account_id,
            "contractId": contract_id,
            "orderId": exit_order_id,
            "side": 1,  # ProjectX compatibility: 1=SELL
            "size": size,
            "price": exit_price,
            "profitAndLoss": gross_pnl,
            "fees": fees_total / 2,
            "creationTimestamp": exit_time.astimezone(timezone.utc).isoformat(),
            "voided": False,
            "raw_source": "sim_broker_dry_run",
        },
    ]


def _patch_api_for_dry_run(api, trades: List[Dict[str, Any]], capture_session: CaptureSession, fake_supabase: FakeSupabaseClient):
    broker_calls: List[str] = []

    def fake_broker_post(path: str, payload: dict):
        broker_calls.append(path)
        logging.info("[dry-run] Fake broker call path=%s payload=%s", path, payload)
        if path != "/api/Trade/search":
            raise RuntimeError(f"Dry run forbids broker path {path}; expected only /api/Trade/search")
        return {"success": True, "trades": trades}

    patch_state = PatchState(
        api=api,
        original_post=api.post,
        original_session=api.session,
        original_get_supabase_client=api.get_supabase_client,
        original_supabase_url=api.SUPABASE_URL,
        original_supabase_key=api.SUPABASE_KEY,
        original_accounts=dict(api.ACCOUNTS),
    )

    api.post = fake_broker_post
    api.session = capture_session
    api.get_supabase_client = lambda: fake_supabase
    api.SUPABASE_URL = DRY_SUPABASE_URL
    api.SUPABASE_KEY = DRY_SUPABASE_KEY
    api.ACCOUNTS = {DEFAULT_ACCOUNT_NAME: DEFAULT_ACCOUNT_ID, **dict(api.ACCOUNTS)}
    return patch_state, broker_calls


def run_dry_fill(
    *,
    output_path: Optional[str | Path] = None,
    log_level: str = "INFO",
    account_name: str = DEFAULT_ACCOUNT_NAME,
    account_id: int = DEFAULT_ACCOUNT_ID,
    contract_id: str = DEFAULT_CONTRACT_ID,
    entry_order_id: str = DEFAULT_ENTRY_ORDER_ID,
    exit_order_id: str = DEFAULT_EXIT_ORDER_ID,
    size: int = 1,
    entry_price: float = 5000.0,
    exit_price: float = 5008.5,
    gross_pnl: float = 42.5,
    fees_total: float = 2.4,
) -> Dict[str, Any]:
    """Run one synthetic entry/exit fill through api.log_trade_results_to_supabase()."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("[dry-run] Starting simulated broker fill; no live broker/Supabase network calls will be made")

    api = _safe_import_api()

    now = datetime.now(api.MT)
    entry_time = now - timedelta(minutes=2)
    exit_time = now - timedelta(minutes=1)
    trace_id = f"sim-dry-run-{int(entry_time.timestamp())}"
    ai_decision_id = 900001

    trades = _build_simulated_projectx_trades(
        account_id=account_id,
        contract_id=contract_id,
        entry_order_id=entry_order_id,
        exit_order_id=exit_order_id,
        entry_time=entry_time,
        exit_time=exit_time,
        size=size,
        entry_price=entry_price,
        exit_price=exit_price,
        gross_pnl=gross_pnl,
        fees_total=fees_total,
    )

    fake_supabase = FakeSupabaseClient()
    capture_session = CaptureSession()
    patch_state, broker_calls = _patch_api_for_dry_run(api, trades, capture_session, fake_supabase)

    meta = {
        "strategy": "sim_broker_dry_run",
        "signal": "BUY",
        "symbol": contract_id,
        "account": account_name,
        "size": size,
        "order_id": entry_order_id,
        "entry_price": entry_price,
        "alert": "dry-run simulated broker fill",
        "comment": "Generated by scripts/dry_run_simulated_fill.py; no live broker order placed",
        "session_id": "SIM-SESSION-0001",
        "trace_id": trace_id,
        "prompt_version": "dry-run-v1",
        "exit_trigger": "sim_broker_synthetic_flat",
        "exit_reason": "Synthetic exit fill for broker compatibility layer Milestone 1",
        "exit_signal": "FLAT",
        "exit_requested_at": exit_time.isoformat(),
    }

    try:
        api.log_trade_results_to_supabase(
            acct_id=account_id,
            cid=contract_id,
            entry_time=entry_time,
            ai_decision_id=ai_decision_id,
            meta=meta,
        )
    finally:
        patch_state.restore()

    if not capture_session.inserts:
        raise RuntimeError("Dry run did not reach the Supabase insert capture path")

    payload = capture_session.inserts[0]["json"]
    result = {
        "dry_run": True,
        "live_broker_calls": False,
        "signalr_required": False,
        "broker_calls": broker_calls,
        "supabase_queries": fake_supabase.queries,
        "supabase_updates": fake_supabase.updates,
        "supabase_inserts": capture_session.inserts,
        "payload": payload,
    }

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        logging.info("[dry-run] Wrote captured dry-run result to %s", out)

    logging.info(
        "[dry-run] Completed. Captured trade_results payload: account=%s symbol=%s total_pnl=%s net_pnl=%s trace_id=%s",
        payload.get("account"),
        payload.get("symbol"),
        payload.get("total_pnl"),
        payload.get("net_pnl"),
        payload.get("trace_id"),
    )
    return result


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate one simulated broker fill and dry-run it through the legacy trade_results logging path."
    )
    parser.add_argument("--output", dest="output_path", help="Optional path for captured JSON payload/result")
    parser.add_argument("--log-level", default="INFO", help="Python logging level, default INFO")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    result = run_dry_fill(output_path=args.output_path, log_level=args.log_level)
    payload = result["payload"]
    print(json.dumps({
        "dry_run": True,
        "live_broker_calls": False,
        "signalr_required": False,
        "broker_calls": result["broker_calls"],
        "supabase_insert_url": result["supabase_inserts"][0]["url"],
        "trade_results_payload_preview": {
            "account": payload.get("account"),
            "symbol": payload.get("symbol"),
            "strategy": payload.get("strategy"),
            "signal": payload.get("signal"),
            "size": payload.get("size"),
            "entry_price": payload.get("entry_price"),
            "exit_price": payload.get("exit_price"),
            "total_pnl": payload.get("total_pnl"),
            "fees_total": payload.get("fees_total"),
            "net_pnl": payload.get("net_pnl"),
            "trace_id": payload.get("trace_id"),
            "raw_trades_count": len(payload.get("raw_trades") or []),
        },
        "output_path": args.output_path,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
