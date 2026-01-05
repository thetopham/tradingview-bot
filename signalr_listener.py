import os
import time
import threading
import logging
from datetime import datetime, timedelta

import pytz
from dateutil import parser
from signalrcore.hub_connection_builder import HubConnectionBuilder

from api import ACCOUNTS, search_pos, log_trade_results_to_supabase

MT = pytz.timezone("America/Denver")
USER_HUB_URL_BASE = "wss://rtc.topstepx.com/hubs/user?access_token={}"

orders_state = {}
positions_state = {}
trade_meta = {}
recent_closures = {}

def _build_trace_id(entry_time, ai_decision_id, order_id=None, session_id=None):
    try:
        if isinstance(entry_time, (int, float)):
            ts = int(entry_time)
        elif isinstance(entry_time, str):
            ts = int(parser.isoparse(entry_time).timestamp())
        else:
            ts = int(getattr(entry_time, "timestamp", lambda: time.time())())
    except Exception:
        ts = int(time.time())

    base = ai_decision_id if ai_decision_id is not None else "no_ai_id"
    suffix = str(order_id or session_id or "unknown")
    return f"{base}-{suffix}-{ts}"


def track_trade(
    acct_id,
    cid,
    entry_time,
    ai_decision_id,
    strategy,
    sig,
    size,
    order_id,
    alert,
    account,
    symbol,
    sl_id=None,
    tp_ids=None,
    trades=None,
    regime=None,
):
    """Enhanced trade tracking with session ID to prevent mixing trades"""

    import uuid

    session_id = str(uuid.uuid4())[:8]

    meta = {
        "session_id": session_id,
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
        "regime": regime,
        "trace_id": _build_trace_id(entry_time, ai_decision_id, order_id=order_id, session_id=session_id),
    }

    logging.info(
        f"[track_trade] New session {session_id} - AI decision {ai_decision_id}, "
        f"order {order_id}, {sig} {size} {symbol}, trace_id={meta['trace_id']}"
    )

    trade_meta[(acct_id, cid)] = meta


def reconstruct_trade_metadata_on_startup():
    """Reconstruct trade metadata for open positions after restart"""
    from api import ACCOUNTS
    import uuid

    logging.info("Reconstructing trade metadata for open positions...")
    reconstructed_count = 0

    for account_name, acct_id in ACCOUNTS.items():
        try:
            positions = search_pos(acct_id)

            for pos in positions:
                if pos.get("size", 0) > 0:
                    cid = pos["contractId"]
                    creation_time = pos.get("creationTimestamp")

                    if (acct_id, cid) in trade_meta:
                        continue

                    position_type = pos.get("type")
                    if position_type == 1:
                        signal = "BUY"
                    elif position_type == 2:
                        signal = "SELL"
                    else:
                        signal = "UNKNOWN"

                    session_id = str(uuid.uuid4())[:8]
                    trace_id = _build_trace_id(
                        creation_time,
                        f"RESTART_{int(time.time())}",
                        order_id=None,
                        session_id=session_id,
                    )

                    meta = {
                        "entry_time": creation_time,
                        "ai_decision_id": f"RESTART_{int(time.time())}",
                        "strategy": "unknown_restart",
                        "signal": signal,
                        "size": pos["size"],
                        "order_id": None,
                        "sl_id": None,
                        "tp_ids": None,
                        "alert": f"Position reconstructed after restart",
                        "account": account_name,
                        "symbol": cid,
                        "trades": None,
                        "regime": "unknown",
                        "comment": f"Metadata reconstructed on {datetime.now(MT).strftime('%Y-%m-%d %H:%M:%S')}",
                        "session_id": session_id,
                        "trace_id": trace_id,
                    }

                    trade_meta[(acct_id, cid)] = meta
                    reconstructed_count += 1

                    logging.warning(
                        "Reconstructed metadata for %s: %s contracts %s @ $%.2f",
                        account_name,
                        pos["size"],
                        signal,
                        pos.get("averagePrice", 0),
                    )

        except Exception as e:
            logging.error("Failed to reconstruct metadata for %s: %s", account_name, e)

    if reconstructed_count > 0:
        logging.info("Successfully reconstructed metadata for %s open positions", reconstructed_count)
    else:
        logging.info("No open positions found that need metadata reconstruction")

    return reconstructed_count

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

                    if int(time.time()) % 5 == 0:
                        if self.hub and hasattr(self.hub, "transport"):
                            state = getattr(self.hub.transport, "state", None)
                            if state and state.value != 1:
                                logging.warning(f"Connection unhealthy (state={state}), reconnecting...")
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
        self.hub = (
            HubConnectionBuilder()
            .with_url(
                url,
                options={
                    "access_token_factory": lambda: token,
                    "headers": {"Authorization": f"Bearer {token}", "User-Agent": "TradingBot/1.0"},
                },
            )
            .configure_logging(logging.INFO)
            .with_automatic_reconnect(
                {"type": "interval", "intervals": [0, 1, 2, 5, 10, 30], "keep_alive_interval": 15, "reconnect_interval": 5}
            )
            .build()
        )

        def wrap_handler(handler):
            def wrapped(*args, **kwargs):
                self.last_event_time = time.time()
                return handler(*args, **kwargs)

            return wrapped

        self.hub.on("GatewayUserAccount", wrap_handler(self.event_handlers.get("on_account_update", self.default_handler)))
        self.hub.on("GatewayUserOrder", wrap_handler(self.event_handlers.get("on_order_update", self.default_handler)))
        self.hub.on("GatewayUserPosition", wrap_handler(self.event_handlers.get("on_position_update", self.default_handler)))
        self.hub.on("GatewayUserTrade", wrap_handler(self.event_handlers.get("on_trade_update", self.default_handler)))

        self.hub.on_open(lambda: self.on_open())
        self.hub.on_close(lambda: self.handle_close())
        self.hub.on_reconnect(self.on_reconnected)
        self.hub.on_error(lambda err: self.handle_error(err))

        max_start_attempts = 3
        for attempt in range(max_start_attempts):
            try:
                self.hub.start()
                time.sleep(3)

                if hasattr(self.hub, "transport") and hasattr(self.hub.transport, "state"):
                    if self.hub.transport.state.value == 1:
                        logging.info("SignalR hub started successfully")
                        self.last_event_time = time.time()
                        return
                    logging.warning(f"Hub state after start: {self.hub.transport.state}")

            except Exception as e:
                logging.error(f"SignalR start attempt {attempt + 1}/{max_start_attempts} failed: {e}")
                if attempt < max_start_attempts - 1:
                    wait_time = (attempt + 1) * 5
                    logging.info(f"Waiting {wait_time}s before retry...")
                    time.sleep(wait_time)
                else:
                    raise

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
        self.sweep_and_cleanup_positions_and_stops()

    def sweep_and_cleanup_positions_and_stops(self):
        """Clean up any stale metadata after a reconnect."""
        try:
            removed = cleanup_stale_metadata()
            logging.info("Stale metadata cleanup complete; removed %s entries", removed)
        except Exception as exc:
            logging.error("Error during stale metadata cleanup: %s", exc)

    def default_handler(self, args):
        self.last_event_time = time.time()
        logging.info(f"SignalR event: {args}")

    def handle_close(self):
        close_time = datetime.now(MT)
        logging.warning(f"SignalR connection closed at {close_time}")

        if close_time.minute >= 25 and close_time.minute <= 30:
            logging.info("Disconnect near hour boundary - waiting 90s for server reset")
            time.sleep(90)
        else:
            time.sleep(10)

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

    def handle_error(self, err):
        logging.error(f"SignalR connection error: {err}")
        error_msg = str(err).lower()

        if "401" in error_msg or "unauthorized" in error_msg:
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

    
# --- Event Handlers ---

def on_account_update(args):
    logging.info(f"[Account Update] {args}")

def on_order_update(args):
    # Always unwrap if data is present
    order = args[0] if isinstance(args, list) and args else args
    order_data = order.get("data") if isinstance(order, dict) and "data" in order else order

    account_id = order_data.get("accountId")
    contract_id = order_data.get("contractId")
    status = order_data.get("status")
    if account_id is None or contract_id is None:
        logging.error(f"on_order_update: missing account_id or contract_id in {order_data}")
        return

    orders_state.setdefault(account_id, {})[order_data.get("id")] = order_data

    

    if status == 2:
        meta = trade_meta.setdefault((account_id, contract_id), {})
        if "entry_time" not in meta or not meta["entry_time"]:
            meta["entry_time"] = order_data.get("creationTimestamp") or time.time()
        if "order_id" not in meta or not meta["order_id"]:
            meta["order_id"] = order_data.get("id")
        logging.info(f"Order filled: {order_data}")
        logging.info(f"[on_order_update] meta after update: {meta}")


def on_position_update(args):
    # Always unwrap if data is present
    position = args[0] if isinstance(args, list) and args else args
    position_data = position.get("data") if isinstance(position, dict) and "data" in position else position

    account_id = position_data.get("accountId")
    contract_id = position_data.get("contractId")
    size = position_data.get("size", 0)
    if account_id is None or contract_id is None:
        logging.error(f"on_position_update: missing account_id or contract_id in {position_data}")
        return

    positions_state.setdefault(account_id, {})[contract_id] = position_data

    entry_time = position_data.get("creationTimestamp")
    if size > 0:
        meta = trade_meta.get((account_id, contract_id))

        if meta is not None:
            if not meta.get("trace_id"):
                meta["trace_id"] = _build_trace_id(
                    meta.get("entry_time", entry_time),
                    meta.get("ai_decision_id"),
                    order_id=meta.get("order_id"),
                    session_id=meta.get("session_id"),
                )

            meta_entry_time = meta.get("entry_time")

            try:
                if isinstance(meta_entry_time, str):
                    meta_time = parser.isoparse(meta_entry_time)
                else:
                    meta_time = datetime.fromtimestamp(meta_entry_time, MT)

                current_time = parser.isoparse(entry_time) if entry_time else datetime.now(MT)

                time_diff = abs((current_time - meta_time).total_seconds())

                if time_diff > 60:
                    logging.warning(f"Metadata appears to be from old position (age: {time_diff:.0f}s), clearing")
                    meta = None
                    trade_meta.pop((account_id, contract_id), None)
                else:
                    meta["entry_time"] = entry_time
            except Exception as e:
                logging.error(f"Error comparing timestamps: {e}")

        if meta is None:
            logging.warning(
                f"[on_position_update] No meta for open position acct={account_id}, cid={contract_id}. Creating basic metadata."
            )
            import uuid

            position_type = position_data.get("type")
            signal = "BUY" if position_type == 1 else "SELL" if position_type == 2 else "UNKNOWN"

            account_name = "unknown"
            for name, id in ACCOUNTS.items():
                if id == account_id:
                    account_name = name
                    break

            session_id = str(uuid.uuid4())[:8]

            trade_meta[(account_id, contract_id)] = {
                "entry_time": entry_time or time.time(),
                "ai_decision_id": None,
                "strategy": "manual",
                "signal": signal,
                "size": size,
                "order_id": None,
                "sl_id": None,
                "tp_ids": None,
                "alert": "Position tracked by SignalR",
                "account": account_name,
                "symbol": contract_id,
                "trades": None,
                "regime": "unknown",
                "comment": f"Metadata created on position update at {datetime.now(MT).strftime('%Y-%m-%d %H:%M:%S')}",
                "session_id": session_id,
                "trace_id": _build_trace_id(entry_time, None, session_id=session_id),
            }
    

    if size == 0:
        last_close = recent_closures.get((account_id, contract_id))
        if last_close and time.time() - last_close < 5:
            logging.warning(
                "[on_position_update] Duplicate close detected within 5s for acct=%s cid=%s; skipping log",
                account_id,
                contract_id,
            )
            return

        recent_closures[(account_id, contract_id)] = time.time()
        meta = trade_meta.pop((account_id, contract_id), None)
        logging.info(f"[on_position_update] Position closed for acct={account_id}, cid={contract_id}")
        logging.info(f"[on_position_update] meta at close: {meta}")

        if meta:
            ai_decision_id = meta.get("ai_decision_id")
            logging.info(f"[on_position_update] Calling log_trade_results_to_supabase with ai_decision_id={ai_decision_id}")
            log_trade_results_to_supabase(
                acct_id=account_id,
                cid=contract_id,
                entry_time=meta.get("entry_time"),
                ai_decision_id=ai_decision_id,
                meta=meta,
            )
        else:
            logging.warning(f"[on_position_update] No meta found for closed position, creating minimal log entry")

            last_position = positions_state.get(account_id, {}).get(contract_id, {})

            import uuid

            session_id = str(uuid.uuid4())[:8]
            trace_id = _build_trace_id(entry_time, None, session_id=session_id)

            account_name = "unknown"
            for name, id in ACCOUNTS.items():
                if id == account_id:
                    account_name = name
                    break

            minimal_meta = {
                "strategy": "unknown",
                "signal": "UNKNOWN",
                "symbol": contract_id,
                "account": account_name,
                "size": last_position.get("size", 0),
                "alert": "Position closed without metadata",
                "comment": "Trade result logged without original metadata",
                "session_id": session_id,
                "trace_id": trace_id,
            }

            entry_time = last_position.get("creationTimestamp", datetime.now(MT) - timedelta(hours=1))

            log_trade_results_to_supabase(
                acct_id=account_id,
                cid=contract_id,
                entry_time=entry_time,
                ai_decision_id=None,
                meta=minimal_meta,
            )

        
        
def on_trade_update(args):
    logging.info(f"[Trade Update] {args}")


def cleanup_stale_metadata(max_age_hours=24):
    """Remove old metadata entries that might be orphaned"""
    current_time = time.time()
    stale_keys = []

    for key, meta in trade_meta.items():
        entry_time = meta.get("entry_time")
        if entry_time:
            try:
                if isinstance(entry_time, (int, float)):
                    age_hours = (current_time - entry_time) / 3600
                elif isinstance(entry_time, str):
                    entry_dt = parser.isoparse(entry_time)
                    age_hours = (datetime.now(entry_dt.tzinfo) - entry_dt).total_seconds() / 3600
                else:
                    continue

                if age_hours > max_age_hours:
                    stale_keys.append(key)
                    logging.warning(
                        f"Found stale metadata: session {meta.get('session_id')}, age {age_hours:.1f} hours"
                    )
            except Exception as e:
                logging.error(f"Error checking metadata age: {e}")

    for key in stale_keys:
        meta = trade_meta.pop(key, None)
        if meta:
            logging.info(f"Removed stale metadata for session {meta.get('session_id')}")

    return len(stale_keys)

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

    reconstruct_trade_metadata_on_startup()

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




# Example usage:
if __name__ == "__main__":
    print("SignalR Listener module. Import and launch from your tradingview_projectx_bot.py main script.")
