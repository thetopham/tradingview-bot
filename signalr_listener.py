import os
import time
import threading
import logging
from api import search_pos, log_trade_results_to_supabase, check_for_phantom_orders
from datetime import datetime
from signalrcore.hub_connection_builder import HubConnectionBuilder

USER_HUB_URL_BASE = "wss://rtc.topstepx.com/hubs/user?access_token={}"

orders_state = {}
positions_state = {}
trade_meta = {}

def track_trade(acct_id, cid, entry_time, ai_decision_id, strategy, sig, size, order_id, alert, account, symbol, sl_id=None, tp_ids=None, trades=None):
    meta = {
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
    logging.info(f"[track_trade] Called with ai_decision_id={ai_decision_id}, meta={meta}")
    trade_meta[(acct_id, cid)] = meta

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

    if order_data.get("type") == 1 and status == 2:  # TP filled
        logging.info(
            f"Take-profit order filled for acct={account_id}, cid={contract_id}: {order_data}"
        )
    if status == 2:
        # Only add missing fields to meta, never overwrite!
        meta = trade_meta.setdefault((account_id, contract_id), {})
        if "entry_time" not in meta or not meta["entry_time"]:
            meta["entry_time"] = order_data.get("creationTimestamp") or time.time()
        if "order_id" not in meta or not meta["order_id"]:
            meta["order_id"] = order_data.get("id")
        logging.info(f"Order filled: {order_data}")
        logging.info(f"[on_order_update] meta after update: {meta}")


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

    entry_time = position_data.get("creationTimestamp")
    if size > 0:
        meta = trade_meta.get((account_id, contract_id))
        
        # IMPORTANT: Check if this is a NEW position by comparing timestamps
        if meta is not None:
            # If we have metadata, check if it's for THIS position or an old one
            meta_entry_time = meta.get("entry_time")
            
            # Convert times for comparison
            try:
                if isinstance(meta_entry_time, str):
                    meta_time = parser.isoparse(meta_entry_time)
                else:
                    meta_time = datetime.fromtimestamp(meta_entry_time, CT)
                    
                current_time = parser.isoparse(entry_time) if entry_time else datetime.now(CT)
                
                # If the metadata is older than 60 seconds, it's probably from a previous position
                time_diff = abs((current_time - meta_time).total_seconds())
                
                if time_diff > 60:
                    logging.warning(f"Metadata appears to be from old position (age: {time_diff:.0f}s), clearing")
                    meta = None
                    trade_meta.pop((account_id, contract_id), None)
                else:
                    # Update entry time if needed
                    meta["entry_time"] = entry_time
            except Exception as e:
                logging.error(f"Error comparing timestamps: {e}")
        
        if meta is None:
            # CREATE METADATA FOR POSITIONS WITHOUT IT
            logging.warning(f"[on_position_update] No meta for open position acct={account_id}, cid={contract_id}. Creating basic metadata.")
            # Determine position type
            position_type = position_data.get('type')
            signal = 'BUY' if position_type == 1 else 'SELL' if position_type == 2 else 'UNKNOWN'
            
            # Find account name
            account_name = 'unknown'
            for name, id in ACCOUNTS.items():
                if id == account_id:
                    account_name = name
                    break
            
            trade_meta[(account_id, contract_id)] = {
                'entry_time': entry_time or time.time(),
                'ai_decision_id': None,  # No AI decision for manual trades
                'strategy': 'manual',
                'signal': signal,
                'size': size,
                'order_id': None,
                'sl_id': None,
                'tp_ids': None,
                'alert': 'Position tracked by SignalR',
                'account': account_name,
                'symbol': contract_id,
                'trades': None,
                'regime': 'unknown',
                'comment': f'Metadata created on position update at {datetime.now(CT).strftime("%Y-%m-%d %H:%M:%S")}'
            }

    # DELAY the ensure_stops_match_position call to allow orders to settle
    # This is key to preventing premature stop cancellation
    def delayed_stop_check():
        time.sleep(2)  # Wait 2 seconds for orders to register
        ensure_stops_match_position(account_id, contract_id)
    
    # Run in separate thread to not block
    threading.Thread(target=delayed_stop_check, daemon=True).start()

    if size == 0:
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
                meta=meta
            )
        else:
            # STILL LOG TRADE RESULTS EVEN WITHOUT METADATA
            logging.warning(f"[on_position_update] No meta found for closed position, creating minimal log entry")
            
            # Try to get position info from the last known state
            last_position = positions_state.get(account_id, {}).get(contract_id, {})
            
            # Find account name
            account_name = 'unknown'
            for name, id in ACCOUNTS.items():
                if id == account_id:
                    account_name = name
                    break
            
            # Create minimal metadata for logging
            minimal_meta = {
                'strategy': 'unknown',
                'signal': 'UNKNOWN',
                'symbol': contract_id,
                'account': account_name,
                'size': last_position.get('size', 0),
                'alert': 'Position closed without metadata',
                'comment': 'Trade result logged without original metadata'
            }
            
            # Use position creation time if available
            entry_time = last_position.get('creationTimestamp', datetime.now(CT) - timedelta(hours=1))
            
            log_trade_results_to_supabase(
                acct_id=account_id,
                cid=contract_id,
                entry_time=entry_time,
                ai_decision_id=None,
                meta=minimal_meta
            )
            
        check_for_phantom_orders(account_id, contract_id)


# Also add a helper function to clean up stale metadata

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
                    logging.warning(f"Found stale metadata: session {meta.get('session_id')}, "
                                  f"age {age_hours:.1f} hours")
            except Exception as e:
                logging.error(f"Error checking metadata age: {e}")
    
    # Remove stale entries
    for key in stale_keys:
        meta = trade_meta.pop(key, None)
        if meta:
            logging.info(f"Removed stale metadata for session {meta.get('session_id')}")
    
    return len(stale_keys)

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
    

def ensure_stops_match_position(acct_id, contract_id, max_retries=5, retry_delay=0.4):
    from api import search_open, place_stop, cancel, search_pos
    
    if acct_id is None or contract_id is None:
        logging.error(f"ensure_stops_match_position called with acct_id={acct_id}, contract_id={contract_id} (BUG IN CALLER)")
        return
    
    # Wait longer initially to let things settle
    time.sleep(3)  # Add initial delay
    
    for attempt in range(max_retries):
        position = positions_state.get(acct_id, {}).get(contract_id)
        if position is None:
            fresh_positions = search_pos(acct_id)
            position = next((p for p in fresh_positions if p["contractId"] == contract_id), None)
            if position:
                positions_state.setdefault(acct_id, {})[contract_id] = position
        
        current_size = position.get("size", 0) if position else 0
        if current_size > 0 or attempt == max_retries - 1:
            break
        time.sleep(retry_delay)
    
    open_orders = search_open(acct_id)
    stops = [o for o in open_orders if o["contractId"] == contract_id and o["type"] == 4 and o["status"] == 1]
    
    # Get position type to determine stop side
    position_type = position.get("type") if position else None
    
    # Only sync stops if we have a position
    if current_size > 0:
        for stop in stops:
            if stop["size"] != current_size:
                logging.info(f"[SL SYNC] Adjusting stop from size {stop['size']} to match position {current_size}")
                cancel(acct_id, stop["id"])
                
                # Only replace if we know the stop price
                stop_price = stop.get("stopPrice")
                if stop_price and position_type:
                    stop_side = 1 if position_type == 1 else 0
                    time.sleep(0.5)
                    place_stop(acct_id, contract_id, stop_side, current_size, stop_price)
    
    # Only cancel stops if we're REALLY sure there's no position
    elif current_size == 0:
        # Check if stop is very new (might be for a position we haven't seen yet)
        for stop in stops:
            # Check stop age if possible
            stop_time = stop.get("creationTimestamp")
            if stop_time:
                try:
                    from dateutil import parser
                    from datetime import datetime, timezone
                    stop_dt = parser.parse(stop_time)
                    age_seconds = (datetime.now(timezone.utc) - stop_dt).total_seconds()
                    
                    # Don't cancel stops less than 10 seconds old
                    if age_seconds < 10:
                        logging.info(f"[SL SYNC] Keeping new stop {stop['id']} (age: {age_seconds:.1f}s)")
                        continue
                except:
                    pass
            
            logging.info(f"[SL SYNC] No position found, canceling old stop {stop['id']}")
            cancel(acct_id, stop["id"])


# Example usage:
if __name__ == "__main__":
    print("SignalR Listener module. Import and launch from your tradingview_projectx_bot.py main script.")
