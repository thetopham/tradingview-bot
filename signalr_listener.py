import os
import time
import threading
import logging
from signalrcore.hub_connection_builder import HubConnectionBuilder

# Assumes these globals/functions are imported from your main bot:
# - ACCOUNTS (dict: name -> id)
# - authenticate() (sets and returns _token, _token_expiry)
# - _token, _token_expiry, auth_lock

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# SignalR endpoint for TopstepX/ProjectX
USER_HUB_URL_BASE = "wss://rtc.topstepx.com/hubs/user?access_token={}"

class SignalRTradingListener(threading.Thread):
    def __init__(self, accounts, authenticate_func, token_getter, token_expiry_getter, auth_lock, event_handlers=None):
        super().__init__(daemon=True)
        self.accounts = list(accounts.values())
        self.authenticate_func = authenticate_func
        self.token_getter = token_getter
        self.token_expiry_getter = token_expiry_getter
        self.auth_lock = auth_lock
        self.event_handlers = event_handlers or {}
        self.hub = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.last_token = None

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.ensure_token_valid()
                token = self.token_getter()
                if token != self.last_token:
                    self.last_token = token
                self.connect_signalr(token)
                self.stop_event.wait(3600)  # sleep until token nears expiry or thread is stopped
            except Exception as e:
                logging.error(f"SignalRListener error: {e}", exc_info=True)
                time.sleep(10)  # Avoid hammering the server

    def ensure_token_valid(self):
        # Call this before (re)connecting; will re-auth if needed.
        with self.auth_lock:
            if self.token_expiry_getter() - time.time() < 60:  # less than 1 min left
                logging.info("Refreshing JWT token for SignalR connection.")
                self.authenticate_func()

    def connect_signalr(self, token):
        url = USER_HUB_URL_BASE.format(token)
        if self.hub:
            self.hub.stop()
        self.hub = (
            HubConnectionBuilder()
            .with_url(url, options={
                "access_token_factory": lambda: token,
                "headers": {"Authorization": f"Bearer {token}"},
            })
            .configure_logging(logging.INFO)
            .with_automatic_reconnect({
                "type": "raw",
                "keep_alive_interval": 10,
                "reconnect_interval": 5
            })
            .build()
        )
        # Register all event handlers
        self.hub.on("GatewayUserAccount", self.event_handlers.get("on_account_update", self.default_handler))
        self.hub.on("GatewayUserOrder", self.event_handlers.get("on_order_update", self.default_handler))
        self.hub.on("GatewayUserPosition", self.event_handlers.get("on_position_update", self.default_handler))
        self.hub.on("GatewayUserTrade", self.event_handlers.get("on_trade_update", self.default_handler))

        self.hub.on_open(lambda: logging.info("SignalR connection established."))
        self.hub.on_close(lambda: logging.info("SignalR connection closed."))
        self.hub.on_reconnected(self.on_reconnected)
        self.hub.on_error(lambda err: logging.error(f"SignalR connection error: {err}"))

        self.hub.start()
        self.subscribe_all()

    def subscribe_all(self):
        self.hub.send("SubscribeAccounts")
        self.hub.send("SubscribeOrders", self.accounts)
        self.hub.send("SubscribePositions", self.accounts)
        self.hub.send("SubscribeTrades", self.accounts)
        logging.info(f"Subscribed to accounts/orders/positions/trades for: {self.accounts}")

    def on_reconnected(self):
        logging.info("SignalR reconnected! Resubscribing to all events...")
        self.subscribe_all()

    def default_handler(self, args):
        logging.info(f"SignalR event: {args}")

    def stop(self):
        self.stop_event.set()
        if self.hub:
            self.hub.stop()

# Example event handler functions you can plug into your trade logic
def on_account_update(args):
    logging.info(f"[Account Update] {args}")

def on_order_update(args):
    logging.info(f"[Order Update] {args}")
    # TODO: Update your internal order state, log fills/cancels, etc.

def on_position_update(args):
    logging.info(f"[Position Update] {args}")
    # TODO: Update your internal position state, trigger SL/TP handlers, etc.

def on_trade_update(args):
    logging.info(f"[Trade Update] {args}")
    # TODO: Log or trigger anything trade-related

# Usage example to launch listener from your main bot:
def launch_signalr_listener():
    # Import or pass references to your shared state/auth functions
    from tradingview_projectx_bot import (
        ACCOUNTS, authenticate, _token, _token_expiry, auth_lock
    )

    def get_token():
        return _token

    def get_token_expiry():
        return _token_expiry

    event_handlers = {
        "on_account_update": on_account_update,
        "on_order_update": on_order_update,
        "on_position_update": on_position_update,
        "on_trade_update": on_trade_update,
    }
    listener = SignalRTradingListener(
        ACCOUNTS,
        authenticate_func=authenticate,
        token_getter=get_token,
        token_expiry_getter=get_token_expiry,
        auth_lock=auth_lock,
        event_handlers=event_handlers
    )
    listener.start()
    return listener

# If you want to run this as a standalone listener for testing:
if __name__ == "__main__":
    from tradingview_projectx_bot import (
        ACCOUNTS, authenticate, _token, _token_expiry, auth_lock
    )

    def get_token():
        return _token

    def get_token_expiry():
        return _token_expiry

    event_handlers = {
        "on_account_update": on_account_update,
        "on_order_update": on_order_update,
        "on_position_update": on_position_update,
        "on_trade_update": on_trade_update,
    }
    authenticate()  # Ensure token is valid at startup!
    listener = SignalRTradingListener(
        ACCOUNTS,
        authenticate_func=authenticate,
        token_getter=get_token,
        token_expiry_getter=get_token_expiry,
        auth_lock=auth_lock,
        event_handlers=event_handlers
    )
    listener.start()
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        listener.stop()
