"""Simulation broker with persistent JSON state.

Implements a minimal ProjectX-compatible surface for simulated accounts,
orders, positions, trades, and bracket links. This module is intentionally
self-contained and not wired into api.py yet.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

SIM_STATE_PATH = "sim_state.json"

_STATE_LOCK = threading.RLock()


def _now_iso_utc() -> str:
    """Return current UTC time as ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _round_to_tick(price: float, tick_size: float) -> float:
    """Round a price to the nearest tick size."""
    if tick_size == 0:
        return price
    return round(price / tick_size) * tick_size


def _load_state(state_path: str) -> dict[str, Any]:
    """Load the simulation state from disk, returning defaults if missing."""
    if not os.path.exists(state_path):
        return {
            "nextOrderId": 1,
            "nextPositionId": 1,
            "nextTradeId": 1,
            "accounts": [],
            "orders": [],
            "positions": [],
            "trades": [],
            "last_processed_bar_ts": {},
            "bracket_links": {},
        }

    with open(state_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _save_state_atomic(state_path: str, state: dict[str, Any]) -> None:
    """Persist state to disk using atomic replace."""
    tmp_path = f"{state_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(tmp_path, state_path)


class SimBroker:
    """Persistent simulation broker for ProjectX-style API requests."""

    def __init__(self, state_path: str, config: dict[str, Any], price_feed: Any | None = None) -> None:
        self.state_path = state_path
        self.config = config
        self.price_feed = price_feed

    def _load_state_locked(self) -> dict[str, Any]:
        with _STATE_LOCK:
            return _load_state(self.state_path)

    def _save_state_locked(self, state: dict[str, Any]) -> None:
        with _STATE_LOCK:
            _save_state_atomic(self.state_path, state)

    def handle_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Route /api/... POST endpoints for the simulation broker."""
        if path == "/api/sim/update":
            account_id = payload.get("accountId")
            contract_id = payload.get("contractId")
            now_ts_iso = payload.get("now_ts_iso")
            if account_id is None or contract_id is None:
                return {
                    "success": False,
                    "errorCode": "INVALID_ARGUMENT",
                    "errorMessage": "accountId and contractId are required",
                }
            return self.sim_update(account_id, contract_id, now_ts_iso)

        return {
            "success": False,
            "errorCode": "NOT_IMPLEMENTED",
            "errorMessage": f"No sim broker handler for path: {path}",
        }

    def sim_update(self, accountId: int, contractId: str, now_ts_iso: str | None = None) -> dict[str, Any]:
        """Update simulated state for an account/contract tuple."""
        now_ts = now_ts_iso or _now_iso_utc()

        with _STATE_LOCK:
            state = _load_state(self.state_path)
            state.setdefault("last_processed_bar_ts", {})
            state.setdefault("bracket_links", {})
            state.setdefault("orders", [])
            state.setdefault("positions", [])
            state.setdefault("trades", [])
            state.setdefault("accounts", [])
            state.setdefault("nextOrderId", 1)
            state.setdefault("nextPositionId", 1)
            state.setdefault("nextTradeId", 1)

            key = f"{accountId}:{contractId}"
            state["last_processed_bar_ts"][key] = now_ts

            _save_state_atomic(self.state_path, state)

        return {
            "success": True,
            "errorCode": None,
            "errorMessage": None,
            "accountId": accountId,
            "contractId": contractId,
            "lastProcessedBarTs": now_ts,
        }

    def get_state_snapshot(self) -> dict[str, Any]:
        """Return a copy of the current state (debug helper)."""
        with _STATE_LOCK:
            state = _load_state(self.state_path)
        return copy.deepcopy(state)
