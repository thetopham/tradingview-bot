import os
import time
import threading
import logging
from datetime import datetime
from typing import Dict

import pytz
from signalrcore.hub_connection_builder import HubConnectionBuilder

from api import log_trade_results_to_supabase
from config import load_config

config = load_config()
CT = config['CT']
USER_HUB_URL_BASE = "wss://rtc.topstepx.com/hubs/user?access_token={}"
ACCOUNTS = config['ACCOUNTS']
ACCOUNT_NAME_BY_ID = {v: k for k, v in ACCOUNTS.items()}

orders_state: Dict = {}
positions_state: Dict = {}


class SignalRTradingListener(threading.Thread):
    def __init__(self, accounts, authenticate_func, token_getter, token_expiry_getter, auth_lock, event_handlers=None):
        super().__init__(daemon=True)
        self.accounts = accounts
        self.authenticate_func = authenticate_func
        self.token_getter = token_getter
        self.token_expiry_getter = token_expiry_getter
        self.auth_lock = auth_lock
        self.event_handlers = event_handlers or {}
        self.hub = None
        self.stop_event = threading.Event()
        self.reconnect_event = threading.Event()
        self.last_token = None
        self.last_event_time = time.time()

    def run(self):
        consecutive_failures = 0

        while not self.stop_event.is_set():
            try:
                self.ensure_token_valid()
                token = self.token_getter()

                if token != self.last_token:
                    self.last_token = token
                    logging.info("Token changed, establishing new connection")

                self.connect_signalr(token)
                consecutive_failures = 0

                while not self.stop_event.is_set():
                    if self.reconnect_event.wait(timeout=1):
                        self.reconnect_event.clear()
                        logging.info("Reconnect event triggered")
                        break

            except Exception as e:
                consecutive_failures += 1
                wait_time = min(60 * consecutive_failures, 300)
                logging.error(f"SignalRListener error (attempt {consecutive_failures}): {e}", exc_info=True)
                logging.info(f"Waiting {wait_time}s before retry...")
                time.sleep(wait_time)

    def ensure_token_valid(self):
        with self.auth_lock:
            if self.token_expiry_getter() - time.time() < 60:
                logging.info("Refreshing JWT token for SignalR connection.")
                self.authenticate_func()

    def connect_signalr(self, token):
        if not token:
            logging.error("No token available for SignalR connection! Check authentication.")
            return

        logging.info(f"Using token for SignalR (first 8): {token[:8]}...")

        if self.hub:
            try:
                self.hub.stop()
                time.sleep(2)
            except Exception as e:
                logging.debug(f"Error stopping old hub: {e}")
            self.hub = None

        url = USER_HUB_URL_BASE.format(token)

        def wrap_handler(handler):
            def wrapped(*args, **kwargs):
                self.last_event_time = time.time()
                return handler(*args, **kwargs)
            return wrapped

        self.hub = (
            HubConnectionBuilder()
            .with_url(url, options={
                "access_token_factory": lambda: token,
                "headers": {
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "TradingBot/1.0",
                },
            })
            .configure_logging(logging.INFO)
            .with_automatic_reconnect({
                "type": "interval",
                "intervals": [0, 1, 2, 5, 10, 30],
                "keep_alive_interval": 15,
                "reconnect_interval": 5,
            })
            .build()
        )

        self.hub.on("GatewayUserAccount", wrap_handler(self.event_handlers.get("on_account_update", self.default_handler)))
        self.hub.on("GatewayUserOrder", wrap_handler(self.event_handlers.get("on_order_update", self.default_handler)))
        self.hub.on("GatewayUserPosition", wrap_handler(self.event_handlers.get("on_position_update", self.default_handler)))
        self.hub.on("GatewayUserTrade", wrap_handler(self.event_handlers.get("on_trade_update", self.default_handler)))

        self.hub.on_open(lambda: self.on_open())
        self.hub.on_close(lambda: self.handle_close())
        self.hub.on_reconnect(self.on_reconnected)
        self.hub.on_error(lambda err: self.handle_error(err))

        self.hub.start()

    def on_open(self):
        logging.info("SignalR connection established. Subscribing to all events.")
        self.last_event_time = time.time()
        self.subscribe_all()

    def subscribe_all(self):
        self.hub.send("SubscribeAccounts", [])
        for acct_id in self.accounts:
            logging.info(f"Subscribing for account: {acct_id}")
            self.hub.send("SubscribeOrders", [acct_id])
            self.hub.send("SubscribePositions", [acct_id])
            self.hub.send("SubscribeTrades", [acct_id])
        logging.info(f"Subscribed to accounts/orders/positions/trades for: {self.accounts}")

    def on_reconnected(self):
        logging.info("SignalR reconnected! Resubscribing to all events...")
        self.last_event_time = time.time()
        self.subscribe_all()

    def default_handler(self, args):
        self.last_event_time = time.time()
        logging.info(f"SignalR event: {args}")

    def handle_close(self):
        close_time = datetime.now(CT)
        logging.warning(f"SignalR connection closed at {close_time}")
        time.sleep(10)
        self.reconnect_event.set()
        if self.hub:
            try:
                self.hub.stop()
            except Exception:
                pass

    def handle_error(self, err):
        logging.error(f"SignalR connection error: {err}")
        error_msg = str(err).lower()

        if '401' in error_msg or 'unauthorized' in error_msg:
            logging.error("Authentication error - forcing token refresh")
            with self.auth_lock:
                self.authenticate_func()

        time.sleep(30)
        self.reconnect_event.set()
        if self.hub:
            try:
                self.hub.stop()
            except Exception:
                pass

    def stop(self):
        self.stop_event.set()
        if self.hub:
            self.hub.stop()


def on_account_update(args):
    logging.info(f"[Account Update] {args}")


def on_order_update(args):
    logging.info(f"[Order Update] {args}")


def on_position_update(args):
    position = args[0] if isinstance(args, list) and args else args
    position_data = position.get("data") if isinstance(position, dict) and "data" in position else position

    account_id = position_data.get("accountId")
    contract_id = position_data.get("contractId")
    size = position_data.get("size", 0)
    if account_id is None or contract_id is None:
        logging.error(f"on_position_update: missing account_id or contract_id in {position_data}")
        return

    positions_state.setdefault(account_id, {})[contract_id] = position_data

    if size == 0:
        account_name = ACCOUNT_NAME_BY_ID.get(account_id, "unknown")
        entry_time = position_data.get("creationTimestamp")
        logging.info(f"[Position Closed] account={account_name} cid={contract_id}")
        log_trade_results_to_supabase(
            acct_id=account_id,
            cid=contract_id,
            entry_time=entry_time,
            ai_decision_id=None,
            meta={
                "account": account_name,
                "symbol": contract_id,
                "strategy": "reduction",
                "signal": "UNKNOWN",
                "alert": "signalr_close",
            },
        )


def on_trade_update(args):
    logging.info(f"[Trade Update] {args}")


def parse_account_ids_from_env():
    result = []
    for k, v in os.environ.items():
        if k.startswith("ACCOUNT_"):
            try:
                if v.isdigit():
                    result.append(int(v))
            except Exception:
                continue
    return result


def launch_signalr_listener(get_token, get_token_expiry, authenticate, auth_lock):
    accounts = parse_account_ids_from_env()
    logging.info(f"Parsed accounts from env: {accounts}")

    event_handlers = {
        "on_account_update": on_account_update,
        "on_order_update": on_order_update,
        "on_position_update": on_position_update,
        "on_trade_update": on_trade_update,
    }

    listener = SignalRTradingListener(
        accounts=accounts,
        authenticate_func=authenticate,
        token_getter=get_token,
        token_expiry_getter=get_token_expiry,
        auth_lock=auth_lock,
        event_handlers=event_handlers,
    )
    listener.start()
    return listener


if __name__ == "__main__":
    print("SignalR Listener module. Import and launch from your tradingview_projectx_bot.py main script.")
