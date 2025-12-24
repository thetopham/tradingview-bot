import logging
import threading
import time
from datetime import datetime

from signalrcore.hub_connection_builder import HubConnectionBuilder

from api import log_trade_results_to_supabase
from config import load_config

config = load_config()
CT = config['CT']
USER_HUB_URL_BASE = "wss://rtc.topstepx.com/hubs/user?access_token={}" 
ACCOUNTS = list(config['ACCOUNTS'].values())

logger = logging.getLogger(__name__)


class SignalRTradingListener(threading.Thread):
    def __init__(self, accounts, authenticate_func, token_getter, token_expiry_getter, auth_lock):
        super().__init__(daemon=True)
        self.accounts = accounts
        self.authenticate_func = authenticate_func
        self.token_getter = token_getter
        self.token_expiry_getter = token_expiry_getter
        self.auth_lock = auth_lock
        self.hub = None
        self.stop_event = threading.Event()
        self.last_event_time = time.time()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.ensure_token_valid()
                token = self.token_getter()
                self.connect_signalr(token)

                while not self.stop_event.is_set():
                    time.sleep(1)
            except Exception as exc:
                logger.error("SignalR listener error: %s", exc, exc_info=True)
                time.sleep(10)

    def ensure_token_valid(self):
        with self.auth_lock:
            if self.token_expiry_getter() - time.time() < 60:
                logger.info("Refreshing JWT token for SignalR connection.")
                self.authenticate_func()

    def connect_signalr(self, token):
        if not token:
            logger.error("No token available for SignalR connection! Check authentication.")
            return

        if self.hub:
            try:
                self.hub.stop()
                time.sleep(1)
            except Exception:
                pass

        url = USER_HUB_URL_BASE.format(token)
        self.hub = (
            HubConnectionBuilder()
            .with_url(url, options={
                "access_token_factory": lambda: token,
                "headers": {"Authorization": f"Bearer {token}"},
            })
            .configure_logging(logging.INFO)
            .with_automatic_reconnect({"type": "interval", "keep_alive_interval": 10, "intervals": [0, 2, 5, 10]})
            .build()
        )

        self.hub.on("GatewayUserPosition", self.on_position_update)
        self.hub.on("GatewayUserTrade", self.on_trade_update)
        self.hub.on_open(lambda: self.on_open())
        self.hub.on_close(lambda: self.on_close())
        self.hub.on_error(lambda err: logger.error("SignalR hub error: %s", err))

        self.hub.start()

    def on_open(self):
        logger.info("SignalR connection established. Subscribing to events.")
        self.last_event_time = time.time()
        self.hub.send("SubscribeAccounts", [])
        for acct_id in self.accounts:
            self.hub.send("SubscribePositions", [acct_id])
            self.hub.send("SubscribeTrades", [acct_id])

    def on_close(self):
        logger.warning("SignalR connection closed; retrying soon")
        time.sleep(5)

    def stop(self):
        self.stop_event.set()
        if self.hub:
            self.hub.stop()

    def on_position_update(self, args):
        position = args[0] if isinstance(args, list) and args else args
        position_data = position.get("data") if isinstance(position, dict) and "data" in position else position
        account_id = position_data.get("accountId") if isinstance(position_data, dict) else None
        contract_id = position_data.get("contractId") if isinstance(position_data, dict) else None
        size = position_data.get("size") if isinstance(position_data, dict) else None
        self.last_event_time = time.time()

        logger.info(
            "[SignalR Position] acct=%s cid=%s size=%s ts=%s",
            account_id,
            contract_id,
            size,
            position_data.get("creationTimestamp") if isinstance(position_data, dict) else None,
        )

        if isinstance(size, (int, float)) and size == 0 and account_id and contract_id:
            meta = {
                "strategy": "signalr_log",
                "signal": "UNKNOWN",
                "account": account_id,
                "symbol": contract_id,
                "alert": "Position closed",
            }
            entry_time = position_data.get("creationTimestamp") if isinstance(position_data, dict) else None
            log_trade_results_to_supabase(
                acct_id=account_id,
                cid=contract_id,
                entry_time=entry_time or datetime.now(CT),
                ai_decision_id=None,
                meta=meta,
            )

    def on_trade_update(self, args):
        trade = args[0] if isinstance(args, list) and args else args
        trade_data = trade.get("data") if isinstance(trade, dict) and "data" in trade else trade
        logger.info("[SignalR Trade] %s", trade_data)
        self.last_event_time = time.time()


def launch_signalr_listener(get_token, get_token_expiry, authenticate, auth_lock):
    listener = SignalRTradingListener(ACCOUNTS, authenticate, get_token, get_token_expiry, auth_lock)
    listener.start()
    return listener

