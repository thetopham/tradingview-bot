# scheduler.py

import logging
import time
from datetime import datetime

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config import load_config
from position_manager import PositionManager
from api import (
    get_supabase_client,
    get_market_conditions_summary,
    check_contract_rollover,
    get_contract,
)
from state import get_last_event_id, set_last_event_id


config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']
CT = pytz.timezone("America/Chicago")
TV_PORT = config['TV_PORT']
ACCOUNTS = config['ACCOUNTS']
AUTOTRADE_ACCOUNTS = config.get('AUTOTRADE_ACCOUNTS', list(ACCOUNTS.keys()))

# Global position manager instance
position_manager = None


def start_scheduler(app):
    global position_manager
    scheduler = BackgroundScheduler()

    # Initialize position manager
    position_manager = PositionManager(ACCOUNTS)

    # ──────────────────────────────────────────────────────────────────────────────
    # 5-minute cron job: compute local market state
    # ──────────────────────────────────────────────────────────────────────────────
    def cron_job():
        """Runs after each 5-min candle close to refresh local market state."""
        from auth import in_get_flat
        now = datetime.now(CT)

        if in_get_flat(now):
            logging.info("[APScheduler] SKIPPING market-state refresh - in get-flat window")
            return

        try:
            summary = get_market_conditions_summary(
                force_refresh=True,
                symbol=config.get('DEFAULT_SYMBOL', 'MES'),
                bars_needed=90,
            )
            logging.info(
                "[APScheduler] 5m market state: %s (conf=%s signal=%s)",
                summary.get('regime'),
                summary.get('confidence'),
                summary.get('market_state', {}).get('signal', 'HOLD'),
            )
        except Exception as e:
            logging.error(f"[APScheduler] Market-state refresh failed: {e}")

    # ──────────────────────────────────────────────────────────────────────────────
    # Auto-trade job - runs every 5 minutes after market state refresh
    # ──────────────────────────────────────────────────────────────────────────────
    def auto_trade_job():
        symbol = config.get('DEFAULT_SYMBOL', 'MES')

        try:
            from auth import in_get_flat

            if in_get_flat(datetime.now(CT)):
                logging.info("[AutoTrade] Skipping auto-trade - in get-flat window")
                return
        except Exception as exc:
            logging.error("[AutoTrade] Get-flat check failed: %s", exc)
            return

        if not config.get('AUTOTRADE_ENABLED'):
            logging.info("[AutoTrade] Disabled by AUTOTRADE_ENABLED=false")
            return

        overseer_url = config.get('N8N_OVERSEER_URL', '')
        if not overseer_url:
            logging.info("[AutoTrade] Overseer URL not configured; skipping")
            return

        try:
            summary = get_market_conditions_summary(
                force_refresh=False,
                symbol=symbol,
            )
        except Exception as exc:
            logging.error("[AutoTrade] Failed to load market summary: %s", exc)
            return

        market_state = summary.get('market_state') or summary
        confluence = summary.get('confluence') or {}
        score = float(confluence.get('score', 0) or 0)
        confluence_ok = bool(confluence.get('trade_recommended'))
        require_confluence = config.get('AUTOTRADE_REQUIRE_CONFLUENCE', True)
        min_score = float(config.get('AUTOTRADE_MIN_SCORE', 1.0))
        summary_trade_recommended = bool(summary.get('trade_recommended', False))

        if require_confluence:
            should_trade = confluence_ok and abs(score) >= min_score
        else:
            should_trade = summary_trade_recommended or (confluence_ok and abs(score) >= min_score)

        try:
            cid = get_contract(symbol)
        except Exception as exc:
            logging.error("[AutoTrade] Unable to resolve contract for %s: %s", symbol, exc)
            return

        market_state_ts = market_state.get('timestamp') or summary.get('timestamp')
        if market_state_ts is not None:
            event_id = f"{symbol}:{market_state_ts}"
        else:
            event_id = f"{symbol}:{int(time.time() // 300)}"

        logging.info(
            "[AutoTrade] AUTO 5m event_id=%s score=%.2f min=%.2f conf_ok=%s require_conf=%s summary_recommended=%s tradeable=%s",
            event_id,
            score,
            min_score,
            confluence_ok,
            require_confluence,
            summary_trade_recommended,
            should_trade,
        )

        for account_name in AUTOTRADE_ACCOUNTS:
            if account_name not in ACCOUNTS:
                logging.warning("[AutoTrade] Unknown account '%s' in AUTOTRADE_ACCOUNTS", account_name)
                continue

            acct_id = ACCOUNTS[account_name]

            try:
                account_state = position_manager.get_account_state(acct_id)
            except Exception as exc:
                logging.error("[AutoTrade] %s account state error: %s", account_name, exc)
                continue

            if not account_state.get('can_trade', False):
                logging.info("[AutoTrade] %s skip: risk can_trade=false", account_name)
                continue

            position_context = position_manager.get_position_context_for_ai(acct_id, cid) or {}
            position_context["account_metrics"] = {
                "can_trade": bool(account_state.get("can_trade", False)),
                "risk_level": account_state.get("risk_level", "unknown"),
                "consecutive_losses": int(account_state.get("consecutive_losses") or 0),
            }
            has_position = position_context.get('current_position', {}).get('has_position')

            should_call = should_trade or has_position
            if not should_call:
                logging.info(
                    "[AutoTrade] %s skip: no setup and flat (event_id=%s)",
                    account_name,
                    event_id,
                )
                continue

            if get_last_event_id(account_name) == event_id:
                logging.info(
                    "[AutoTrade] %s skip: idempotent event already processed (event_id=%s)",
                    account_name,
                    event_id,
                )
                continue

            risk_context = {
                'can_trade': account_state.get('can_trade'),
                'risk_level': account_state.get('risk_level'),
                'daily_pnl': account_state.get('daily_pnl'),
                'consecutive_losses': account_state.get('consecutive_losses'),
            }

            payload = {
                'alert': 'AUTO_5M',
                'symbol': symbol,
                'account': account_name,
                'ai_decision_id': event_id,
                'market_state': market_state,
                'confluence': confluence,
                'position_context': position_context,
                'risk_context': risk_context,
                'chart_urls': {},
            }
            decision = {}
            try:
                resp = requests.post(overseer_url, json=payload, timeout=10)
                resp.raise_for_status()
                try:
                    decision = resp.json()
                except ValueError:
                    logging.error("[AutoTrade] %s overseer JSON parse failed", account_name)
                    decision = {}
            except Exception as exc:
                logging.error("[AutoTrade] Overseer call failed for %s: %s", account_name, exc)
                decision = {}

            signal = str(decision.get('signal', 'HOLD') or 'HOLD').upper()
            if signal not in {'BUY', 'SELL', 'HOLD', 'FLAT'}:
                logging.info("[AutoTrade] %s skip: invalid signal=%s", account_name, signal)
                signal = 'HOLD'
            reason = decision.get('reason', '')
            strategy = decision.get('strategy', 'simple') or 'simple'

            size = decision.get('size', config.get('AUTOTRADE_SIZE', 1))
            try:
                size = int(size)
            except Exception:
                size = config.get('AUTOTRADE_SIZE', 1)
            size = max(0, min(size, 3))

            logging.info(
                "[AutoTrade] %s decision=%s size=%s reason=%s has_position=%s",
                account_name,
                signal,
                size,
                reason,
                has_position,
            )

            if signal == 'HOLD' or signal not in {'BUY', 'SELL', 'FLAT'}:
                logging.info("[AutoTrade] %s skip: signal=%s", account_name, signal)
                set_last_event_id(account_name, event_id)
                continue

            current_side = (position_context.get('current_position', {}).get('side') or '').upper()

            if has_position and signal in {'BUY', 'SELL'}:
                logging.info(
                    "[AutoTrade] %s skip: in position (%s), blocking %s to avoid reversal",
                    account_name,
                    current_side,
                    signal,
                )
                set_last_event_id(account_name, event_id)
                continue

            if signal == 'FLAT':
                if has_position:
                    try:
                        from api import flatten_contract

                        if flatten_contract(acct_id, cid, timeout=10):
                            logging.info("[AutoTrade] %s flattened position on %s", account_name, symbol)
                        else:
                            logging.error("[AutoTrade] %s flatten failed for %s", account_name, symbol)
                    except Exception as exc:
                        logging.error("[AutoTrade] %s flatten exception: %s", account_name, exc)
                else:
                    logging.info("[AutoTrade] %s FLAT signal but already flat", account_name)

                set_last_event_id(account_name, event_id)
                continue

            if (signal == 'BUY' and current_side == 'LONG') or (signal == 'SELL' and current_side == 'SHORT'):
                logging.info(
                    "[AutoTrade] %s skip: already aligned with %s",
                    account_name,
                    signal,
                )
                set_last_event_id(account_name, event_id)
                continue

            if size == 0:
                logging.info("[AutoTrade] %s skip: size=0", account_name)
                set_last_event_id(account_name, event_id)
                continue

            side = 'buy' if signal == 'BUY' else 'sell'

            try:
                from tradingview_projectx_bot import execute_internal_webhook

                result = execute_internal_webhook({
                    'secret': WEBHOOK_SECRET,
                    'account': account_name,
                    'strategy': strategy,
                    'symbol': symbol,
                    'side': side,
                    'size': size,
                    'ai_decision_id': event_id,
                    'alert': reason or 'AUTO_5M',
                })
                logging.info(
                    "[AutoTrade] %s executed=%s (event_id=%s)",
                    account_name,
                    result,
                    event_id,
                )
            except Exception as exc:
                logging.error("[AutoTrade] %s execution error: %s", account_name, exc)

            set_last_event_id(account_name, event_id)

    # ──────────────────────────────────────────────────────────────────────────────
    # Market analysis job - runs every 5 minutes using cached state
    # ──────────────────────────────────────────────────────────────────────────────
    def market_analysis_job():
        """Log market conditions and alerts"""
        from auth import in_get_flat
        if in_get_flat(datetime.now(CT)):
            logging.info("[Market Analysis] Skipping - in get-flat window")
            return

        try:
            from api import get_market_conditions_summary
            summary = get_market_conditions_summary(cached_only=True)

            regime = summary.get('regime', 'sideways')
            signal = summary.get('market_state', {}).get('signal', 'HOLD')
            conf_obj = summary.get('confluence', {})
            logging.info(
                "Confluence: bias=%s score=%.2f gates=%s",
                conf_obj.get('bias', 'HOLD'),
                float(conf_obj.get('score', 0.0)),
                conf_obj.get('gates', {}),
            )

            if regime == 'sideways':
                logging.warning(
                    f"⚠️ SIDEWAYS MARKET: Confidence {summary.get('confidence', 0)} - defaulting to HOLD"
                )

            logging.info(
                f"📊 Market Update: {regime} (conf: {summary.get('confidence', 0)}%) | "
                f"Signal: {signal} | Trade OK: {summary.get('trade_recommended')}"
            )

        except Exception as e:
            logging.error(f"[Market Analysis] Error: {e}")

    # ──────────────────────────────────────────────────────────────────────────────
    # Incremental 1m -> 5m updater - runs every minute
    # ──────────────────────────────────────────────────────────────────────────────
    def incremental_market_update():
        try:
            from api import update_market_state_incremental
            update_market_state_incremental()
        except Exception as e:
            logging.error(f"[Incremental Market Update] Error: {e}")

    # ──────────────────────────────────────────────────────────────────────────────
    # Position monitoring job - runs every 2 minutes
    # ──────────────────────────────────────────────────────────────────────────────
    def position_monitoring_job():
        """Monitor existing positions and log alerts"""
        try:
            logging.info("🔄 Running position monitoring...")

            from api import get_contract
            cid = get_contract('MES')

            for account_name, acct_id in ACCOUNTS.items():
                try:
                    # Get position state
                    position_state = position_manager.get_position_state(acct_id, cid)

                    # If we have a position, log its status
                    if position_state['has_position']:
                        logging.info(
                            f"Position Monitor - {account_name}: "
                            f"{position_state['size']} contracts {position_state['side']}, "
                            f"P&L: ${position_state['current_pnl']:.2f} "
                            f"(unrealized: ${position_state.get('unrealized_pnl', 0):.2f}), "
                            f"Duration: {position_state['duration_minutes']:.0f} min"
                        )

                        # Log alerts for concerning positions
                        if position_state['current_pnl'] < -100:
                            logging.warning(f"⚠️ {account_name}: Large loss ${position_state['current_pnl']:.2f}")

                        if position_state['duration_minutes'] > 120:
                            logging.warning(
                                f"⚠️ {account_name}: Stale position ({position_state['duration_minutes']:.0f} min)"
                            )

                        if len(position_state['stop_orders']) == 0:
                            logging.warning(f"⚠️ {account_name}: No stop loss detected!")

                except Exception as e:
                    logging.error(f"[Position Monitor] Error for {account_name}: {e}")

        except Exception as e:
            logging.error(f"[Position Monitor] General error: {e}")

    # ──────────────────────────────────────────────────────────────────────────────
    # Account health check - runs every 30 minutes
    # ──────────────────────────────────────────────────────────────────────────────
    def account_health_check():
        """Monitor account health and risk metrics"""
        try:
            logging.info("🏥 Running account health check...")

            for account_name, acct_id in ACCOUNTS.items():
                try:
                    account_state = position_manager.get_account_state(acct_id)

                    # Log account metrics
                    logging.info(
                        f"Account {account_name}: "
                        f"Daily P&L: ${account_state['daily_pnl']:.2f} | "
                        f"Win Rate: {account_state['win_rate']:.1%} | "
                        f"Risk: {account_state['risk_level']} | "
                        f"Can Trade: {account_state['can_trade']}"
                    )

                    # Warnings
                    if not account_state['can_trade']:
                        logging.warning(f"⛔ Account {account_name} CANNOT TRADE - Risk limits hit")

                    if account_state['risk_level'] == 'high':
                        logging.warning(f"⚠️ Account {account_name} at HIGH RISK")

                    if account_state['consecutive_losses'] >= 2:
                        logging.warning(
                            f"⚠️ Account {account_name} has {account_state['consecutive_losses']} consecutive losses"
                        )

                except Exception as e:
                    logging.error(f"[Account Health] Error for {account_name}: {e}")

        except Exception as e:
            logging.error(f"[Account Health] General error: {e}")

    # ──────────────────────────────────────────────────────────────────────────────
    # Pre-session analysis jobs
    # ──────────────────────────────────────────────────────────────────────────────
    def pre_session_analysis(session_name):
        """Run analysis before each major session"""
        from auth import in_get_flat
        if in_get_flat(datetime.now(CT)):
            logging.info(f"[Pre-session Analysis] Skipping {session_name} - in get-flat window")
            return

        try:
            logging.info(f"🔔 Pre-{session_name} session analysis starting...")
            summary = get_market_conditions_summary(force_refresh=True)
            regime = summary.get('regime', 'sideways')
            if regime == 'sideways':
                logging.warning(
                    f"⚠️ {session_name} session: sideways conditions detected. Consider standing by."
                )
            else:
                logging.info(
                    f"✅ {session_name} session: {regime} trend in play. Stay within risk limits."
                )

        except Exception as e:
            logging.error(f"[Pre-session Analysis] Error: {e}")

    # ──────────────────────────────────────────────────────────────────────────────
    # Data feed monitor
    # ──────────────────────────────────────────────────────────────────────────────
    def monitor_data_feed():
        """Monitor data feed health and log price updates"""
        try:
            from api import get_current_market_price, get_spread_and_mid_price, get_contract

            price, source = get_current_market_price(max_age_seconds=60000)

            if price:
                price_info = get_spread_and_mid_price()

                # Only log if there's an open position or if market is open
                has_positions = False
                for account_name, acct_id in ACCOUNTS.items():
                    positions = position_manager.get_position_state(acct_id, get_contract("MES"))
                    if positions['has_position']:
                        has_positions = True
                        break

                is_market_closed = "market_closed" in source

                if has_positions or not is_market_closed:
                    if is_market_closed:
                        logging.info(f"📊 Last Market Price (CLOSED): ${price:.2f} from {source}")
                    else:
                        logging.info(
                            f"📊 Market Price: ${price:.2f} from {source} | "
                            f"Range: {price_info.get('low', 'N/A')}-{price_info.get('high', 'N/A')} | "
                            f"1m bar: {price_info.get('range', 0):.2f} pts"
                        )
            else:
                now = datetime.now(CT)
                # Only warn if market should be open
                if not (
                    now.weekday() == 5
                    or (now.weekday() == 6 and now.hour < 17)
                    or (now.weekday() == 4 and now.hour >= 16)
                ):
                    logging.warning("⚠️ No current market price available - data feed may be stale")

        except Exception as e:
            logging.error(f"Data feed monitor error: {e}")

    # ──────────────────────────────────────────────────────────────────────────────
    # Metadata cleanup - runs every hour
    # ──────────────────────────────────────────────────────────────────────────────
    def metadata_cleanup_job():
        """Clean up any orphaned trade metadata"""
        try:
            from signalr_listener import cleanup_stale_metadata
            removed = cleanup_stale_metadata(max_age_hours=12)
            if removed > 0:
                logging.info(f"Cleaned up {removed} stale metadata entries")
        except Exception as e:
            logging.error(f"[Metadata Cleanup] Error: {e}")

    # ──────────────────────────────────────────────────────────────────────────────
    # GET FLAT FUNCTIONS
    # ──────────────────────────────────────────────────────────────────────────────
    def get_flat_job():
        """Automatically flatten all positions at get-flat time"""
        try:
            logging.info("⏰ GET FLAT TIME - Flattening all positions")

            from api import search_pos, flatten_contract, get_contract

            flattened_count = 0
            errors = []

            # Get the contract ID for MES
            cid = get_contract('MES')

            for account_name, acct_id in ACCOUNTS.items():
                try:
                    # Get all open positions
                    positions = search_pos(acct_id)

                    # Find positions for our contract
                    open_positions = [p for p in positions if p["contractId"] == cid and p.get("size", 0) > 0]

                    if open_positions:
                        total_size = sum(p.get("size", 0) for p in open_positions)
                        avg_price = (
                            sum(p.get("averagePrice", 0) * p.get("size", 0) for p in open_positions) / total_size
                            if total_size > 0 else 0
                        )

                        logging.warning(f"🔻 FLATTENING {account_name}: {total_size} contracts @ ${avg_price:.2f}")

                        # Flatten the position
                        success = flatten_contract(acct_id, cid, timeout=15)

                        if success:
                            flattened_count += 1
                            logging.info(f"✅ Successfully flattened {account_name}")

                            # Log the flatten action to Supabase for tracking
                            try:
                                from api import get_supabase_client
                                supabase = get_supabase_client()

                                supabase.table('ai_trading_log').insert({
                                    'strategy': 'get_flat',
                                    'signal': 'FLAT',
                                    'symbol': 'MES',
                                    'account': account_name,
                                    'size': 0,
                                    'timestamp': datetime.now(CT).isoformat(),
                                    'reason': 'Automatic get-flat at 3:07 PM CT',
                                    'alert': 'Scheduled get-flat window',
                                    'ai_decision_id': f'GET_FLAT_{int(time.time())}',
                                }).execute()
                            except Exception as e:
                                logging.error(f"Failed to log get-flat action: {e}")
                        else:
                            errors.append(f"{account_name}: Failed to flatten")
                            logging.error(f"❌ Failed to flatten {account_name}")
                    else:
                        logging.info(f"No open positions for {account_name}")

                except Exception as e:
                    errors.append(f"{account_name}: {str(e)}")
                    logging.error(f"Error processing {account_name}: {e}")

            # Summary
            if flattened_count > 0:
                logging.warning(f"🏁 GET FLAT COMPLETE: Flattened {flattened_count} accounts")
            else:
                logging.info("GET FLAT: No positions to flatten")

            if errors:
                logging.error(f"GET FLAT ERRORS: {', '.join(errors)}")

        except Exception as e:
            logging.error(f"[Get Flat Job] Critical error: {e}")

    def pre_flat_warning_job():
        """Warn 5 minutes before get-flat time"""
        try:
            logging.warning("⚠️ GET FLAT WARNING: Positions will be flattened in 5 minutes (3:07 PM CT)")

            # Check current positions and log warning
            from api import search_pos, get_contract
            cid = get_contract('MES')

            positions_to_flatten = []

            for account_name, acct_id in ACCOUNTS.items():
                positions = search_pos(acct_id)
                open_positions = [p for p in positions if p["contractId"] == cid and p.get("size", 0) > 0]

                if open_positions:
                    total_size = sum(p.get("size", 0) for p in open_positions)
                    positions_to_flatten.append(f"{account_name}: {total_size} contracts")

            if positions_to_flatten:
                logging.warning(f"⚠️ Positions to be flattened at 3:07 PM: {', '.join(positions_to_flatten)}")

        except Exception as e:
            logging.error(f"[Pre-flat Warning] Error: {e}")

    # ──────────────────────────────────────────────────────────────────────────────
    # Schedule jobs
    # ──────────────────────────────────────────────────────────────────────────────

    scheduler.add_job(
        metadata_cleanup_job,
        CronTrigger(minute=30, timezone=CT),  # Run at :30 every hour
        id='metadata_cleanup',
        replace_existing=True,
    )

    # Chart fetch and regime update - every 5 minutes
    scheduler.add_job(
        cron_job,
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=5, timezone=CT),
        id='5m_job',
        replace_existing=True,
    )

    # Auto-trade overseer (runs shortly after the 5m refresh)
    scheduler.add_job(
        auto_trade_job,
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=10, timezone=CT),
        id='auto_trade_job',
        replace_existing=True,
    )

    # Incremental 1m aggregation every minute
    scheduler.add_job(
        incremental_market_update,
        CronTrigger(minute='*', second=20, timezone=CT),
        id='market_state_incremental',
        replace_existing=True,
    )

    # Market analysis every 5 minutes
    scheduler.add_job(
        market_analysis_job,
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=55, timezone=CT),
        id='market_analysis',
        replace_existing=True,
    )

    # Position monitoring every 2 minutes
    scheduler.add_job(
        position_monitoring_job,
        CronTrigger(
            minute='1,3,6,8,11,13,16,18,21,23,26,28,31,33,36,38,41,43,46,48,51,53,56,58',
            second=0,
            timezone=CT,
        ),
        id='position_monitoring',
        replace_existing=True,
    )

    # Data feed monitor - every minute during market hours
    scheduler.add_job(
        monitor_data_feed,
        CronTrigger(minute='*', second=15, timezone=CT),
        id='data_feed_monitor',
        replace_existing=True,
    )

    # Account health check every 30 minutes
    scheduler.add_job(
        account_health_check,
        CronTrigger(minute='0,30', second=45, timezone=CT),
        id='account_health',
        replace_existing=True,
    )

    # Pre-session analysis
    # London session prep (1:45 AM CT)
    scheduler.add_job(
        lambda: pre_session_analysis('LONDON'),
        CronTrigger(hour=1, minute=45, timezone=CT),
        id='pre_london',
        replace_existing=True,
    )

    # NY Morning session prep (8:15 AM CT)
    scheduler.add_job(
        lambda: pre_session_analysis('NY_MORNING'),
        CronTrigger(hour=8, minute=15, timezone=CT),
        id='pre_ny_morning',
        replace_existing=True,
    )

    # NY Afternoon session prep (12:45 PM CT)
    scheduler.add_job(
        lambda: pre_session_analysis('NY_AFTERNOON'),
        CronTrigger(hour=12, minute=45, timezone=CT),
        id='pre_ny_afternoon',
        replace_existing=True,
    )

    # GET FLAT JOBS
    # Pre-flat warning at 3:02 PM CT (5 minutes before)
    scheduler.add_job(
        pre_flat_warning_job,
        CronTrigger(hour=15, minute=2, timezone=CT),
        id='pre_flat_warning',
        replace_existing=True,
    )

    # Get flat at 3:07 PM CT
    scheduler.add_job(
        get_flat_job,
        CronTrigger(hour=15, minute=7, timezone=CT),
        id='get_flat',
        replace_existing=True,
    )

    # Contract rollover check at 6:00 AM CT
    scheduler.add_job(
        check_contract_rollover,
        CronTrigger(hour=6, minute=0, timezone=CT),
        id='contract_rollover_check',
        replace_existing=True,
    )

    scheduler.start()

    autotrade_enabled = config.get('AUTOTRADE_ENABLED')
    overseer_configured = bool(config.get('N8N_OVERSEER_URL'))
    logging.info("[APScheduler] Scheduler started (auto-trade=%s overseer=%s)", autotrade_enabled, overseer_configured)
    logging.info("[AutoTrade] Startup AUTOTRADE_ACCOUNTS=%s", AUTOTRADE_ACCOUNTS)
    logging.info(
        "[AutoTrade] Startup gating: require_confluence=%s min_score=%.2f overseer_configured=%s",
        config.get('AUTOTRADE_REQUIRE_CONFLUENCE', True),
        float(config.get('AUTOTRADE_MIN_SCORE', 1.0)),
        overseer_configured,
    )
    logging.info("[AutoTrade] Startup AUTOTRADE_ENABLED=%s", autotrade_enabled)

    return scheduler
