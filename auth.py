#auth.py

import time
import threading
import logging
from datetime import datetime
import pytz
import requests
from config import load_config

session = requests.Session()

config = load_config()
PX_BASE = config['PX_BASE']
USER_NAME = config['USER_NAME']
API_KEY = config['API_KEY']
GET_FLAT_START = config['GET_FLAT_START']
GET_FLAT_END = config['GET_FLAT_END']
SUNDAY_MARKET_OPEN = config['SUNDAY_MARKET_OPEN']
CT = pytz.timezone("America/Chicago")  # Or load from config if you want


# ─── Auth State ───────────────────────────────────────
_token = None
_token_expiry = 0
auth_lock = threading.Lock()

def in_get_flat(now=None):
    """Return True when trading should remain flat.

    Rules (Mountain Time, converted to Central Time):
    - Monday-Thursday: 2:05pm–4:00pm MT (3:05pm–5:00pm CT)
    - Friday: 2:05pm MT through Sunday market open (5:00pm CT)
    - Saturday: fully flat
    - Sunday: flat until futures reopen
    """

    now = now or datetime.now(CT)
    if hasattr(now, "astimezone"):
        now = now.astimezone(CT)

    current_time = now.timetz().replace(tzinfo=None) if hasattr(now, "timetz") else now
    weekday = now.weekday()  # Monday=0, Sunday=6

    # Monday - Thursday daily get-flat window
    if weekday <= 3:
        return GET_FLAT_START <= current_time <= GET_FLAT_END

    # Friday: start at 2:05pm MT (3:05pm CT) and stay flat into the weekend
    if weekday == 4:
        return current_time >= GET_FLAT_START

    # Saturday: remain flat all day
    if weekday == 5:
        return True

    # Sunday: stay flat until futures market open
    if weekday == 6:
        return current_time < SUNDAY_MARKET_OPEN

    return False

def authenticate():
    global _token, _token_expiry
    logging.info("Authenticating to Topstep API...")
    resp = session.post(
        f"{PX_BASE}/api/Auth/loginKey",
        json={"userName": USER_NAME, "apiKey": API_KEY},
        headers={"Content-Type": "application/json"},
        timeout=(3.05, 10)
    )
    logging.info(f"Topstep response: {resp.status_code} {resp.text}")
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        logging.error("Auth failed: %s", data)
        raise RuntimeError("Auth failed")
    _token = data["token"]
    _token_expiry = time.time() + 23 * 3600
    logging.info(f"Authentication successful; token (first 8): {_token[:8]}... expires in ~23h.")

def get_token():
    return _token

def get_token_expiry():
    return _token_expiry

def ensure_token():
    with auth_lock:
        if _token is None or time.time() >= _token_expiry:
            authenticate()
