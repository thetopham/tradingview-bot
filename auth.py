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
CT = pytz.timezone("America/Denver")  # Mountain Time
WEEKEND_MARKET_OPEN = config['WEEKEND_MARKET_OPEN']


# ─── Auth State ───────────────────────────────────────
_token = None
_token_expiry = 0
auth_lock = threading.Lock()

def in_get_flat(now=None):
    now = now or datetime.now(CT)
    if now.tzinfo:
        now = now.astimezone(CT)
    else:
        now = CT.localize(now)

    t = now.timetz().replace(tzinfo=None)
    weekday = now.weekday()  # Monday=0, Sunday=6

    # Monday–Thursday: respect daily flatten window
    if weekday < 4:
        return GET_FLAT_START <= t <= GET_FLAT_END

    # Friday: block starting at the window start, then stay flat for the weekend
    if weekday == 4:
        return t >= GET_FLAT_START

    # Saturday: always flat
    if weekday == 5:
        return True

    # Sunday: flat until futures market re-opens
    if weekday == 6:
        return t < WEEKEND_MARKET_OPEN

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
