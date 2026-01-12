# auth.py
import os
import threading
import time
import requests
import logging
from config import load_config

config = load_config()
PX_BASE = config['PX_BASE']

# Broker mode (live|sim). In sim mode we never hit ProjectX.
BROKER_MODE = (os.getenv("BROKER_MODE") or config.get("BROKER_MODE") or "live").strip().lower()

session = requests.Session()
_token = None
_token_expiry = 0
auth_lock = threading.Lock()

# Guard used elsewhere to prevent flatten/close loops
in_get_flat = False

def authenticate():
    """Authenticate against ProjectX Gateway (live) or return a dummy token (sim)."""
    global _token, _token_expiry

    if BROKER_MODE == "sim":
        _token = "SIM_TOKEN"
        _token_expiry = time.time() + 3600
        return _token

    payload = {"userName": os.getenv("PROJECTX_USER"), "apiKey": os.getenv("PROJECTX_API_KEY")}
    url = f"{PX_BASE}/api/Auth/loginKey"
    resp = session.post(url, json=payload, timeout=(3.05, 10))
    resp.raise_for_status()
    data = resp.json()
    _token = data.get("token")
    # token valid for 24 hours (or treat as 1h if missing)
    _token_expiry = time.time() + 3600 * 24
    return _token

def ensure_token():
    """Refresh token if needed (live). No-op for sim."""
    global _token_expiry

    if BROKER_MODE == "sim":
        return

    with auth_lock:
        if _token is None or time.time() > (_token_expiry - 60):
            logging.info("Token expired or missing, authenticating...")
            authenticate()

def get_token():
    return _token

def get_token_expiry():
    return _token_expiry
