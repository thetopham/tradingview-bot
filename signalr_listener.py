import os
import time
import threading
import logging
from api import search_pos
from datetime import datetime
from signalrcore.hub_connection_builder import HubConnectionBuilder

USER_HUB_URL_BASE = "wss://rtc.topstepx.com/hubs/user?access_token={}"

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
        self.accounts = accounts  # list of account IDs (ints)
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
        for acct_id in self.accounts:
            logging.info(f"Subscribing for account: {acct_id}")
            self.hub.send("SubscribeOrders", [acct_id])
            self.hub.send("SubscribePositions", [acct_id])
            self.hub.send("SubscribeTrades", [acct_id])
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

# --- Event Handlers ---
def on_account_update(args):
    logging.info(f"[Account Update] {args}")

def on_order_update(args):
    order = args[0] if isinstance(args, list) and args else args
    account_id = order.get("accountId")
    contract_id = order.get("contractId")
    status = order.get("status")
    orders_state.setdefault(account_id, {})[order.get("id")] = order

    if order.get("type") == 1 and status == 2:  # TP filled
        ensure_stops_match_position(account_id, contract_id)

    if status == 2:
        now = time.time()
        trade_meta[(account_id, contract_id)] = {
            "entry_time": now,
            "order_id": order_id,
        }
        logging.info(f"Order filled: {order}")

def on_position_update(args):
    from api import log_trade_results_to_supabase
    position = args[0] if isinstance(args, list) and args else args
    account_id = position.get("accountId")
    contract_id = position.get("contractId")
    size = position.get("size", 0)
    positions_state.setdefault(account_id, {})[contract_id] = position
    ensure_stops_match_position(account_id, contract_id)

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

def parse_account_ids_from_env():
    """
    Parses all ACCOUNT_... variables that are numeric (Topstep account IDs)
    """
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
        event_handlers=event_handlers
    )
    listener.start()
    return listener

def ensure_stops_match_position(acct_id, contract_id):
    from api import search_open, place_stop, cancel
    position = positions_state.get(acct_id, {}).get(contract_id)
    current_size = position.get("size", 0) if position else 0

    open_orders = search_open(acct_id)
    stops = [o for o in open_orders if o["contractId"] == contract_id and o["type"] == 4 and o["status"] == 1]

    for stop in stops:
        if stop["size"] != current_size and current_size > 0:
            logging.info(f"[SL SYNC] Canceling old stop of size {stop['size']} to match position {current_size}")
            cancel(acct_id, stop["id"])
            # Keep stop price the same (improve: recalc if you want dynamic trailing)
            stop_side = stop["side"]
            stop_price = stop.get("stopPrice")
            place_stop(acct_id, contract_id, stop_side, current_size, stop_price)
    if current_size == 0:
        for stop in stops:
            logging.info(f"[SL SYNC] No open position, canceling leftover stop {stop['id']}")
            cancel(acct_id, stop["id"])


# Example usage:
if __name__ == "__main__":
    print("SignalR Listener module. Import and launch from your tradingview_projectx_bot.py main script.")
