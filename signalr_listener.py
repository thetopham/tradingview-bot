import os
import time
import threading
import logging
from api import search_pos, log_trade_results_to_supabase, check_for_phantom_orders
from datetime import datetime
from dateutil import parser
from signalrcore.hub_connection_builder import HubConnectionBuilder
import pytz


CT = pytz.timezone("America/Chicago")
USER_HUB_URL_BASE = "wss://rtc.topstepx.com/hubs/user?access_token={}"

orders_state = {}
positions_state = {}
trade_meta = {}

def track_trade(acct_id, cid, entry_time, ai_decision_id, strategy, sig, size, order_id, 
                alert, account, symbol, sl_id=None, tp_ids=None, trades=None, regime=None):
    """Enhanced trade tracking with session ID to prevent mixing trades"""
    
    # Generate a unique session ID for this trade
    import uuid
    session_id = str(uuid.uuid4())[:8]  # Short ID for this trading session
    
    meta = {
        "session_id": session_id,  # NEW: Unique ID for this trading session
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
    }
    
    logging.info(f"[track_trade] New session {session_id} - AI decision {ai_decision_id}, "
                f"order {order_id}, {sig} {size} {symbol}")
    
    trade_meta[(acct_id, cid)] = meta

def reconstruct_trade_metadata_on_startup():
    """Reconstruct trade metadata for open positions after restart"""
    from api import search_pos, ACCOUNTS
    import time
    
    logging.info("Reconstructing trade metadata for open positions...")
    reconstructed_count = 0
    
    for account_name, acct_id in ACCOUNTS.items():
        try:
            positions = search_pos(acct_id)
            
            for pos in positions:
                if pos.get('size', 0) > 0:
                    cid = pos['contractId']
                    creation_time = pos.get('creationTimestamp')
                    
                    # Skip if we already have metadata (shouldn't happen but just in case)
                    if (acct_id, cid) in trade_meta:
                        continue
                    
                    # Determine position side
                    position_type = pos.get('type')
                    if position_type == 1:
                        signal = 'BUY'
                    elif position_type == 2:
                        signal = 'SELL'
                    else:
                        signal = 'UNKNOWN'
                    
                    # Create reconstructed metadata
                    meta = {
                        'entry_time': creation_time,
                        'ai_decision_id': f'RESTART_{int(time.time())}',
                        'strategy': 'unknown_restart',
                        'signal': signal,
                        'size': pos['size'],
                        'order_id': None,
                        'sl_id': None,
                        'tp_ids': None,
                        'alert': f'Position reconstructed after restart',
                        'account': account_name,
                        'symbol': cid,
                        'trades': None,
                        'regime': 'unknown',
                        'comment': f'Metadata reconstructed on {datetime.now(CT).strftime("%Y-%m-%d %H:%M:%S")}'
                    }
                    
                    trade_meta[(acct_id, cid)] = meta
                    reconstructed_count += 1
                    
                    logging.warning(f"Reconstructed metadata for {account_name}: "
                                  f"{pos['size']} contracts {signal} @ ${pos.get('averagePrice', 0):.2f}")
                    
        except Exception as e:
            logging.error(f"Failed to reconstruct metadata for {account_name}: {e}")
    
    if reconstructed_count > 0:
        logging.info(f"Successfully reconstructed metadata for {reconstructed_count} open positions")
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
        self.reconnect_event = threading.Event()  # For forced reconnects
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
                consecutive_failures = 0  # Reset on success
                
                # Wait for disconnect/reconnect signal
                while not self.stop_event.is_set():
                    if self.reconnect_event.wait(timeout=1):
                        self.reconnect_event.clear()
                        logging.info("Reconnect event triggered")
                        break
                        
                    # Check connection health every 5 seconds
                    if int(time.time()) % 5 == 0:
                        if self.hub and hasattr(self.hub, 'transport'):
                            state = getattr(self.hub.transport, 'state', None)
                            if state and state.value != 1:  # Not connected
                                logging.warning(f"Connection unhealthy (state={state}), reconnecting...")
                                break
                            
                                
            except Exception as e:
                consecutive_failures += 1
                wait_time = min(60 * consecutive_failures, 300)  # Max 5 min
                logging.error(f"SignalRListener error (attempt {consecutive_failures}): {e}", exc_info=True)
                logging.info(f"Waiting {wait_time}s before retry...")
                time.sleep(wait_time)

    def ensure_token_valid(self):
        with self.auth_lock:
            # If token is near expiry, refresh
            if self.token_expiry_getter() - time.time() < 60:
                logging.info("Refreshing JWT token for SignalR connection.")
                self.authenticate_func()

    def connect_signalr(self, token):
        if not token:
            logging.error("No token available for SignalR connection! Check authentication.")
            return
        
        logging.info(f"Using token for SignalR (first 8): {token[:8]}...")
        
        # Clean up any existing connection
        if self.hub:
            try:
                self.hub.stop()
                time.sleep(2)
            except Exception as e:
                logging.debug(f"Error stopping old hub: {e}")
            self.hub = None
        
        url = USER_HUB_URL_BASE.format(token)
        
        # Build hub with better configuration
        self.hub = (
            HubConnectionBuilder()
            .with_url(url, options={
                "access_token_factory": lambda: token,
                "headers": {
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "TradingBot/1.0"
                },
            })
            .configure_logging(logging.INFO)
            .with_automatic_reconnect({
                "type": "interval",
                "intervals": [0, 1, 2, 5, 10, 30],  # Retry intervals
                "keep_alive_interval": 15,  # Increased from 10
                "reconnect_interval": 5    # Increased from 5
            })
            .build()
        )

        def wrap_handler(handler):
            def wrapped(*args, **kwargs):
                self.last_event_time = time.time()
                return handler(*args, **kwargs)
            return wrapped
        
        # Register handlers before starting
        self.hub.on("GatewayUserAccount", wrap_handler(self.event_handlers.get("on_account_update", self.default_handler)))
        self.hub.on("GatewayUserOrder", wrap_handler(self.event_handlers.get("on_order_update", self.default_handler)))
        self.hub.on("GatewayUserPosition", wrap_handler(self.event_handlers.get("on_position_update", self.default_handler)))
        self.hub.on("GatewayUserTrade", wrap_handler(self.event_handlers.get("on_trade_update", self.default_handler)))
        
        self.hub.on_open(lambda: self.on_open())
        self.hub.on_close(lambda: self.handle_close())
        self.hub.on_reconnect(self.on_reconnected)
        self.hub.on_error(lambda err: self.handle_error(err))
        
        # Start with retry logic
        max_start_attempts = 3
        for attempt in range(max_start_attempts):
            try:
                self.hub.start()
                # Wait to verify connection
                time.sleep(3)
                
                # Check if we're actually connected
                if hasattr(self.hub, 'transport') and hasattr(self.hub.transport, 'state'):
                    if self.hub.transport.state.value == 1:  # Connected
                        logging.info("SignalR hub started successfully")
                        self.last_event_time = time.time()
                        return
                    else:
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

    def default_handler(self, args):
        self.last_event_time = time.time()
        logging.info(f"SignalR event: {args}")

    def handle_close(self):
        close_time = datetime.now(CT)
        logging.warning(f"SignalR connection closed at {close_time}")
        
        # Check if this is a market hour boundary (common disconnect time)
        if close_time.minute >= 25 and close_time.minute <= 30:
            logging.info("Disconnect near hour boundary - waiting 90s for server reset")
            time.sleep(90)
        else:
            # Normal reconnect delay
            time.sleep(10)
        
        # Set reconnect flag
        self.reconnect_event.set()
        if self.hub:
            try:
                self.hub.stop()
            except:
                pass

    def handle_error(self, err):
        logging.error(f"SignalR connection error: {err}")
        error_msg = str(err).lower()
        
        # Check for auth errors
        if '401' in error_msg or 'unauthorized' in error_msg:
            logging.error("Authentication error - forcing token refresh")
            with self.auth_lock:
                self.authenticate_func()
        
        # Don't reconnect immediately on errors
        time.sleep(30)
        self.reconnect_event.set()
        if self.hub:
            try:
                self.hub.stop()
            except:
                pass

    def stop(self):
        self.stop_event.set()
        if self.hub:
            self.hub.stop()

# --- Event Handlers ---

def on_account_update(args):
    logging.info(f"[Account Update] {args}")

def on_order_update(args):
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
    
    # Reconstruct metadata for any open positions BEFORE starting listener
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
