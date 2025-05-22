import os
import time
import threading
import logging
from datetime import datetime
from signalrcore.hub_connection_builder import HubConnectionBuilder

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

USER_HUB_URL_BASE = "wss://rtc.topstepx.com/hubs/user?access_token={}"

# --- In-memory live state tracking ---
orders_state = {}      # {account_id: {order_id: order_data}}
positions_state = {}   # {account_id: {contract_id: position_data}}
trade_meta = {}        # {(account_id, contract_id): {entry_time, ai_decision_id, ...}}

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
                self.stop_event.wait(3600)  # Wait; token will refresh before expiry
            except Exception as e:
                logging.error(f"SignalRListener error: {e}", exc_info=True)
                time.sleep(10)

    def ensure_token_valid(self):
        with self.auth_lock:
            if self.token_expiry_getter() - time.time() < 60:
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
        self.hub.on("GatewayUserAccount", self.event_handlers.get("on_account_update", self.default_handler))
        self.hub.on("GatewayUserOrder", self.event_handlers.get("on_order_update", self.default_handler))
        self.hub.on("GatewayUserPosition", self.event_handlers.get("on_position_update", self.default_handler))
        self.hub.on("GatewayUserTrade", self.event_handlers.get("on_trade_update", self.default_handler))
        self.hub.on_open(lambda: logging.info("SignalR connection established."))
        self.hub.on_close(lambda: logging.info("SignalR connection closed."))
        self.hub.on_reconnect(self.on_reconnected)   # <--- FIXED LINE
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

# -- Enhanced handlers for event-driven performance logging! --

def on_account_update(args):
    logging.info(f"[Account Update] {args}")

def on_order_update(args):
    order = args[0] if isinstance(args, list) and args else args
    account_id = order.get("accountId")
    order_id = order.get("id")
    contract_id = order.get("contractId")
    status = order.get("status")  # 2 = filled, 3 = canceled

    # Update order state
    orders_state.setdefault(account_id, {})[order_id] = order

    # If filled and is an entry order, save meta for logging later
    if status == 2:  # Filled
        now = time.time()
        trade_meta[(account_id, contract_id)] = {
            "entry_time": now,
            "order_id": order_id,
        }
        logging.info(f"Order filled: {order}")

def on_position_update(args):
    # Avoid circular import by importing here
    from tradingview_projectx_bot import log_trade_results_to_supabase
    position = args[0] if isinstance(args, list) and args else args
    account_id = position.get("accountId")
    contract_id = position.get("contractId")
    size = position.get("size", 0)
    # Update position state
    positions_state.setdefault(account_id, {})[contract_id] = position

    # Position flattened? Log results.
    if size == 0:
        meta = trade_meta.pop((account_id, contract_id), None)
        if meta:
            logging.info(f"Position flattened, logging trade results: acct={account_id} contract={contract_id}")
            entry_time = meta.get("entry_time")
            ai_decision_id = meta.get("ai_decision_id")
            log_trade_results_to_supabase(
                acct_id=account_id,
                cid=contract_id,
                entry_time=datetime.fromtimestamp(entry_time),
                ai_decision_id=ai_decision_id,
                meta=meta
            )

def on_trade_update(args):
    logging.info(f"[Trade Update] {args}")

def launch_signalr_listener():
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
