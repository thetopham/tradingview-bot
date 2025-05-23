#auth.py

import time
import threading
import logging
from datetime import datetime
import pytz

from state import session  # Only if using shared session, otherwise use requests.Session()
from config import load_config

config = load_config()
PX_BASE = config['PX_BASE']
USER_NAME = config['USER_NAME']
API_KEY = config['API_KEY']
GET_FLAT_START = config['GET_FLAT_START']
GET_FLAT_END = config['GET_FLAT_END']
CT = pytz.timezone("America/Chicago")  # Or load from config if you want


# ─── Auth State ───────────────────────────────────────
_token = None
_token_expiry = 0
auth_lock = threading.Lock()

def in_get_flat(now=None):
    now = now or datetime.now(CT)
    t = now.timetz() if hasattr(now, "timetz") else now
    return GET_FLAT_START <= t <= GET_FLAT_END

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
