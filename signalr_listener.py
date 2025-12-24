import os
import time
import threading
import logging
from datetime import datetime
from signalrcore.hub_connection_builder import HubConnectionBuilder
import pytz

from api import log_trade_results_to_supabase

CT = pytz.timezone("America/Chicago")
USER_HUB_URL_BASE = "wss://rtc.topstepx.com/hubs/user?access_token={}" 

trade_meta = {}


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
                    time.sleep(1)
            except Exception as e:
                consecutive_failures += 1
                wait_time = min(60 * consecutive_failures, 300)
                logging.error("SignalRListener error (attempt %s): %s", consecutive_failures, e, exc_info=True)
                logging.info("Waiting %ss before retry...", wait_time)
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
        if self.hub:
            try:
                self.hub.stop()
                time.sleep(1)
            except Exception as exc:
                logging.debug("Error stopping existing hub: %s", exc)
            self.hub = None

        url = USER_HUB_URL_BASE.format(token)
        self.hub = (
            HubConnectionBuilder()
            .with_url(url, options={"access_token_factory": lambda: token})
            .build()
        )

        self.hub.on("onAccountUpdate", self.event_handlers.get("on_account_update", on_account_update))
        self.hub.on("onOrderUpdate", self.event_handlers.get("on_order_update", on_order_update))
        self.hub.on("onPositionUpdate", self.event_handlers.get("on_position_update", on_position_update))
        self.hub.on("onTradeUpdate", self.event_handlers.get("on_trade_update", on_trade_update))
        self.hub.on_open(lambda: logging.info("SignalR connection opened"))
        self.hub.on_close(self.on_close)
        self.hub.on_error(self.on_error)
        self.hub.start()

    def on_close(self):
        logging.warning("SignalR connection closed. Reconnecting...")
        self.reconnect_event.set()

    def on_error(self, err):
        logging.error("SignalR error: %s", err)
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

    trade_meta[(account_id, contract_id)] = {
        "entry_time": position_data.get("creationTimestamp"),
        "size": size,
    }

    if size == 0:
        meta = trade_meta.pop((account_id, contract_id), {})
        entry_time = meta.get("entry_time")
        logging.info(
            "[Position Update] Position closed for acct=%s cid=%s; logging results.",
            account_id,
            contract_id,
        )
        log_trade_results_to_supabase(
            acct_id=account_id,
            cid=contract_id,
            entry_time=entry_time or datetime.now(CT),
            ai_decision_id=None,
            meta=meta,
        )


def on_trade_update(args):
    logging.info(f"[Trade Update] {args}")


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


if __name__ == "__main__":
    print("SignalR Listener module. Import and launch from your tradingview_projectx_bot.py main script.")
