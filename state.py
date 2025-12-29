#state.py

import json
import logging
import os
import threading
import time

import requests

STATE_FILE = os.path.join(os.path.dirname(__file__), "autotrade_state.json")
_state_lock = threading.Lock()
_state = {
    "last_event": {},
    "last_action_ts": {},
}

session = requests.Session()


def _load_state_from_disk():
    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _state.update({
                "last_event": data.get("last_event", {}),
                "last_action_ts": data.get("last_action_ts", {}),
            })
    except Exception as exc:
        logging.error("Failed to load autotrade state: %s", exc)


def _persist_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f)
    except Exception as exc:
        logging.error("Failed to persist autotrade state: %s", exc)


def get_last_event_id(account: str) -> str | None:
    with _state_lock:
        return _state.get("last_event", {}).get(account)


def set_last_event_id(account: str, event_id: str):
    with _state_lock:
        _state.setdefault("last_event", {})[account] = event_id
        _state.setdefault("last_action_ts", {})[account] = time.time()
        _persist_state()


def get_last_action_ts(account: str) -> float | None:
    with _state_lock:
        return _state.get("last_action_ts", {}).get(account)


def set_last_action_ts(account: str, ts: float | None = None):
    with _state_lock:
        _state.setdefault("last_action_ts", {})[account] = ts or time.time()
        _persist_state()


_load_state_from_disk()
