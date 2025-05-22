import os
import time
import threading
import logging
from datetime import datetime
from signalrcore.hub_connection_builder import HubConnectionBuilder

USER_HUB_URL_BASE = "wss://rtc.topstepx.com/hubs/user?access_token={}"
MARKET_HUB_URL_BASE = "wss://rtc.topstepx.com/hubs/market?access_token={}"

# --- In-memory live state tracking ---
orders_state = {}
positions_state = {}
trade_meta = {}

def track_trade(acct_id, cid, entry_time, ai_decision_id, strategy, sig, size, order_id, alert, account, symbol, sl_id=None, tp_ids=None, trades=None):
    trade_meta[(acct_id, cid)] = {
        "entry_time": entry_time,
        "ai_decision_id": ai_decision_id,
        "strategy": strategy,
        "signal": sig,
        "size": size,
        "order_id": order_id,
        "sl_id": sl_id,
        "tp_ids": tp_ids,
        "alert": alert,
        "account": account,
        "symbol": symbol,
        "trades": trades,
    }

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
        self.last_token = None

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.ensure_token_valid()
                token = self.token_getter()
                if token != self.last_token:
                    self.last_token = token
                self.connect_signalr(token)
                self.stop_event.wait(3600)
            except Exception as e:
                logging.error(f"SignalRListener error: {e}", exc_info=True)
                time.sleep(10)

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

        # Register event handlers
        self.hub.on("GatewayUserAccount", self.event_handlers.get("on_account_update", self.default_handler))
        self.hub.on("GatewayUserOrder", self.event_handlers.get("on_order_update", self.default_handler))
        self.hub.on("GatewayUserPosition", self.event_handlers.get("on_position_update", self.default_handler))
        self.hub.on("GatewayUserTrade", self.event_handlers.get("on_trade_update", self.default_handler))

        self.hub.on_open(lambda: self.on_open())
        self.hub.on_close(lambda: logging.info("SignalR connection closed."))
        self.hub.on_reconnect(self.on_reconnected)
        self.hub.on_error(lambda err: logging.error(f"SignalR connection error: {err}"))
        self.hub.start()

    def on_open(self):
        logging.info("SignalR connection established. Subscribing to all events.")
        self.subscribe_all()

    def subscribe_all(self):
        self.hub.send("SubscribeAccounts", [])
        self.hub.send("SubscribeOrders", [self.accounts])
        self.hub.send("SubscribePositions", [self.accounts])
        self.hub.send("SubscribeTrades", [self.accounts])
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

# --- Market Hub Listener ---
class SignalRMarketListener(threading.Thread):
    def __init__(self, contract_ids, token_getter, token_expiry_getter, auth_lock, event_handlers=None):
        super().__init__(daemon=True)
        self.contract_ids = contract_ids if isinstance(contract_ids, list) else [contract_ids]
        self.token_getter = token_getter
        self.token_expiry_getter = token_expiry_getter
        self.auth_lock = auth_lock
        self.event_handlers = event_handlers or {}
        self.hub = None
        self.stop_event = threading.Event()
        self.last_token = None

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.ensure_token_valid()
                token = self.token_getter()
                if token != self.last_token:
                    self.last_token = token
                self.connect_signalr(token)
                self.stop_event.wait(3600)
            except Exception as e:
                logging.error(f"MarketListener error: {e}", exc_info=True)
                time.sleep(10)

    def ensure_token_valid(self):
        with self.auth_lock:
            if self.token_expiry_getter() - time.time() < 60:
                logging.info("Refreshing JWT token for MarketHub connection.")
                # You may need to call your authenticate function here

    def connect_signalr(self, token):
        if not token:
            logging.error("No token available for MarketHub connection! Check authentication.")
            return
        logging.info(f"Using token for MarketHub (first 8): {token[:8]}...")

        url = MARKET_HUB_URL_BASE.format(token)
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

        # Market data event handlers
        self.hub.on("GatewayQuote", self.event_handlers.get("on_quote", self.default_handler))
        self.hub.on("GatewayTrade", self.event_handlers.get("on_trade", self.default_handler))
        self.hub.on("GatewayDepth", self.event_handlers.get("on_depth", self.default_handler))

        self.hub.on_open(lambda: self.on_open())
        self.hub.on_close(lambda: logging.info("MarketHub connection closed."))
        self.hub.on_reconnect(self.on_reconnected)
        self.hub.on_error(lambda err: logging.error(f"MarketHub connection error: {err}"))
        self.hub.start()

    def on_open(self):
        logging.info("MarketHub connection established. Subscribing to contract events.")
        self.subscribe_all()

    def subscribe_all(self):
        for contract_id in self.contract_ids:
            self.hub.send("SubscribeContractQuotes", [contract_id])
            self.hub.send("SubscribeContractTrades", [contract_id])
            self.hub.send("SubscribeContractMarketDepth", [contract_id])
        logging.info(f"Subscribed to market events for contracts: {self.contract_ids}")

    def on_reconnected(self):
        logging.info("MarketHub reconnected! Resubscribing to all contract events...")
        self.subscribe_all()

    def default_handler(self, *args):
        logging.info(f"MarketHub event: {args}")

    def stop(self):
        self.stop_event.set()
        if self.hub:
            self.hub.stop()

# --- Market Data Event Handlers ---
def on_quote(*args):
    logging.info(f"[Market Quote] {args}")

def on_trade(*args):
    logging.info(f"[Market Trade] {args}")

def on_depth(*args):
    logging.info(f"[Market Depth] {args}")

def launch_market_listener(contract_ids, get_token, get_token_expiry, auth_lock):
    event_handlers = {
        "on_quote": on_quote,
        "on_trade": on_trade,
        "on_depth": on_depth,
    }
    listener = SignalRMarketListener(
        contract_ids,
        token_getter=get_token,
        token_expiry_getter=get_token_expiry,
        auth_lock=auth_lock,
        event_handlers=event_handlers
    )
    listener.start()
    return listener

# If you want to test in standalone mode
if __name__ == "__main__":
    print("SignalR Market Listener module. Import and launch from your trading bot.")
