# position_manager.py
"""
Simplified position context provider for AI trading decisions
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from api import search_pos, search_open, search_trades, get_contract
from config import load_config
from dateutil import parser

config = load_config()
MT = config['MT']

COMBINE_ACCOUNT_SIZE_USD = 50_000
TRAILING_MAX_LOSS_USD = 2_000
DD_SOFT_50_PCT = 0.50
DD_SOFT_75_PCT = 0.75

EQUITY_STATE_PATH = Path(os.environ.get("ACCOUNT_EQUITY_STATE_PATH", "./account_equity_state.json"))
EQUITY_STATE_BAK_PATH = EQUITY_STATE_PATH.with_suffix(EQUITY_STATE_PATH.suffix + ".bak")
_equity_lock = threading.RLock()

class PositionManager:
    """
    Provides position and account context for AI decisions - no autonomous actions
    """
    
    def __init__(self, accounts: Dict[str, int]):
        self.accounts = accounts
        self.logger = logging.getLogger(__name__)
        self._account_state_cache: Dict[int, Tuple[float, Dict]] = {}
        self._equity_state: Dict[str, Dict] = {}

        # Risk parameters (for context only)
        self.max_daily_loss = config.get('MAX_DAILY_LOSS', -500.0)
        self.profit_target = config.get('DAILY_PROFIT_TARGET', 500.0)
        raw_max_consecutive_losses = config.get('MAX_CONSECUTIVE_LOSSES', 3)
        if raw_max_consecutive_losses is not None and raw_max_consecutive_losses <= 0:
            self.logger.info("Consecutive loss guard disabled via MAX_CONSECUTIVE_LOSSES <= 0")
            self.max_consecutive_losses = None
        else:
            self.max_consecutive_losses = raw_max_consecutive_losses
        self.consecutive_loss_guard_enabled = self.max_consecutive_losses is not None

        self._load_equity_state()

    # ------------------------------------------------------------------
    # Persistent equity tracking (for trailing drawdown)
    # ------------------------------------------------------------------
    def _load_equity_state(self):
        with _equity_lock:
            primary_error = None
            data = None

            def _read(path: Path):
                with path.open("r", encoding="utf-8") as f:
                    return json.load(f)

            try:
                if EQUITY_STATE_PATH.exists():
                    data = _read(EQUITY_STATE_PATH)
            except Exception as exc:
                primary_error = exc
                self.logger.warning("Failed to parse equity state from %s: %s", EQUITY_STATE_PATH, exc)

            if data is None and EQUITY_STATE_BAK_PATH.exists():
                try:
                    data = _read(EQUITY_STATE_BAK_PATH)
                    self.logger.warning(
                        "Recovered equity state from backup %s after parse failure", EQUITY_STATE_BAK_PATH
                    )
                except Exception as exc:
                    self.logger.warning("Failed to parse backup equity state from %s: %s", EQUITY_STATE_BAK_PATH, exc)

            if data is None:
                if primary_error:
                    self.logger.warning(
                        "Starting with empty equity state due to parse errors; primary=%s", primary_error
                    )
                self._equity_state = {}
                return

            self._equity_state = data.get("equity_state", {}) or {}
            self.logger.info("Loaded equity state for %s accounts", len(self._equity_state))

    def _save_equity_state(self, force: bool = False):
        with _equity_lock:
            EQUITY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = EQUITY_STATE_PATH.with_suffix(EQUITY_STATE_PATH.suffix + ".tmp")

            payload = {
                "schema_version": 1,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "equity_state": self._equity_state,
            }

            if EQUITY_STATE_PATH.exists():
                try:
                    EQUITY_STATE_BAK_PATH.write_bytes(EQUITY_STATE_PATH.read_bytes())
                except Exception as exc:
                    self.logger.warning("Failed to write backup equity state to %s: %s", EQUITY_STATE_BAK_PATH, exc)

            try:
                with tmp_path.open("w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                os.replace(tmp_path, EQUITY_STATE_PATH)
            except Exception as exc:
                if not force:
                    self.logger.error("Failed to save equity state to %s: %s", EQUITY_STATE_PATH, exc)
                return

    def _get_or_init_equity_record(self, acct_id: int) -> Dict:
        key = str(acct_id)
        record = self._equity_state.get(key)
        if record is None:
            record = {
                "session_start_equity_usd": COMBINE_ACCOUNT_SIZE_USD,
                "equity_peak_usd": COMBINE_ACCOUNT_SIZE_USD,
                "session_started_at": datetime.now(timezone.utc).isoformat(),
            }
            self._equity_state[key] = record
            self._save_equity_state()
        return record

    def get_position_state_light(
        self, acct_id: int, cid: str, *, current_price: Optional[float] = None
    ) -> Dict:
        """Lightweight position snapshot without open order/trade lookups."""

        positions = [p for p in search_pos(acct_id) if p.get("contractId") == cid]

        if not positions:
            return {
                'has_position': False,
                'size': 0,
                'side': None,
                'entry_price': None,
                'current_price': None,
                'current_pnl': 0,
                'unrealized_pnl': 0,
                'duration_minutes': 0,
                'position_type': None,
                'creationTimestamp': None,
            }

        total_size = sum(p.get("size", 0) for p in positions)
        avg_price = (
            sum(p.get("averagePrice", 0) * p.get("size", 0) for p in positions) / total_size
            if total_size > 0
            else 0
        )

        position_type = positions[0].get("type") if positions else None
        side = "LONG" if position_type == 1 else "SHORT" if position_type == 2 else None

        creation_time = positions[0].get("creationTimestamp")
        duration = 0
        if creation_time:
            try:
                from dateutil import parser

                entry_time = parser.parse(creation_time)
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
                duration = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60
            except Exception:
                duration = 0

        try:
            from api import get_current_market_price

            if current_price is None:
                current_price, price_source = get_current_market_price(symbol="MES", max_age_seconds=600)
            else:
                price_source = "supplied"
            contract_multiplier = 5
            if current_price is None:
                unrealized_pnl = 0
            elif side == "LONG":
                unrealized_pnl = (current_price - avg_price) * total_size * contract_multiplier
            elif side == "SHORT":
                unrealized_pnl = (avg_price - current_price) * total_size * contract_multiplier
            else:
                unrealized_pnl = 0
            self.logger.debug(
                "Light P&L calc: side=%s size=%s avg=%.4f px=%s src=%s pnl=%.2f",
                side,
                total_size,
                avg_price,
                current_price,
                price_source,
                unrealized_pnl,
            )
        except Exception as exc:
            self.logger.debug("Light position P&L fallback: %s", exc)
            current_price = None
            unrealized_pnl = 0

        return {
            'has_position': True,
            'size': total_size,
            'side': side,
            'entry_price': avg_price,
            'current_price': current_price,
            'current_pnl': unrealized_pnl,
            'unrealized_pnl': unrealized_pnl,
            'duration_minutes': duration,
            'position_type': position_type,
            'creationTimestamp': creation_time,
        }
    
    def get_position_state(self, acct_id: int, cid: str) -> Dict:
        """
        Get comprehensive position state including P&L, entry price, current stops/targets
        """
        positions = [p for p in search_pos(acct_id) if p["contractId"] == cid]
        open_orders = [o for o in search_open(acct_id) if o["contractId"] == cid]

        if not positions:
            return {
                'has_position': False,
                'size': 0,
                'side': None,
                'entry_price': None,
                'current_pnl': 0,
                'unrealized_pnl': 0,
                'realized_pnl': 0,
                'stop_orders': [],
                'limit_orders': [],
                'duration_minutes': 0
            }

        # Calculate aggregate position
        total_size = sum(p.get("size", 0) for p in positions)
        avg_price = sum(p.get("averagePrice", 0) * p.get("size", 0) for p in positions) / total_size if total_size > 0 else 0

        # Determine side - THIS IS CRITICAL
        position_type = positions[0].get("type") if positions else None
        side = "LONG" if position_type == 1 else "SHORT" if position_type == 2 else None

        # Log for debugging
        self.logger.info(f"Position debug: type={position_type}, side={side}, size={total_size}, avg_price={avg_price}")

        # Get position age
        creation_time = positions[0].get("creationTimestamp")
        entry_time = None
        duration = 0
        if creation_time:
            try:
                from dateutil import parser

                entry_time = parser.parse(creation_time)
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)

                duration = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60
            except Exception:
                entry_time = None
                duration = 0

        # Categorize orders
        stop_orders = [o for o in open_orders if o["type"] == 4 and o["status"] == 1]
        limit_orders = [o for o in open_orders if o["type"] == 1 and o["status"] == 1]

        # IMPORTANT: Only get trades AFTER position entry time to avoid old P&L
        if entry_time:
            try:
                # Only get trades after this position was opened
                trades = search_trades(acct_id, entry_time)
            except Exception:
                trades = []
        else:
            trades = []
    
        # Filter for this contract and calculate realized P&L ONLY for this position
        position_trades = [t for t in trades if t["contractId"] == cid and not t.get("voided", False)]
    
        # Only count trades that are actually closing this position (have P&L)
        realized_pnl = sum(float(t.get("profitAndLoss") or 0) for t in position_trades 
                          if t.get("profitAndLoss") is not None)

        # Calculate unrealized P&L
        unrealized_pnl = 0
        current_price = None

        try:
            from api import get_current_market_price
            current_price, price_source = get_current_market_price(symbol="MES", max_age_seconds=600)
        
            if current_price and avg_price > 0:
                contract_multiplier = 5  # MES multiplier
            
                # Debug logging
                self.logger.info(f"P&L calculation: current={current_price}, entry={avg_price}, "
                               f"size={total_size}, side={side}, type={position_type}")
        
                if side == "LONG":
                    unrealized_pnl = (current_price - avg_price) * total_size * contract_multiplier
                elif side == "SHORT":
                    unrealized_pnl = (avg_price - current_price) * total_size * contract_multiplier
                else:
                    unrealized_pnl = 0
                    self.logger.warning(f"Unknown position side: type={position_type}")
        
                self.logger.info(f"Unrealized P&L: ${unrealized_pnl:.2f}")
                
        except Exception as e:
            self.logger.error(f"Error calculating unrealized P&L: {e}")

        # Total P&L is ONLY unrealized for open positions
        # (realized P&L should be 0 for positions that are still open)
        total_pnl = unrealized_pnl  # Don't add realized_pnl here

        return {
            'has_position': True,
            'size': total_size,
            'side': side,
            'entry_price': avg_price,
            'current_price': current_price,
            'current_pnl': total_pnl,
            'unrealized_pnl': unrealized_pnl,
            'realized_pnl': realized_pnl,
            'stop_orders': stop_orders,
            'limit_orders': limit_orders,
            'duration_minutes': duration,
            'position_type': position_type
        }
    
    def get_account_state(self, acct_id: int) -> Dict:
        """
        Get account-wide state including daily P&L and risk metrics
        """
        equity_record = self._get_or_init_equity_record(acct_id)

        session_start_raw = equity_record.get("session_started_at")
        try:
            session_start_dt = parser.isoparse(session_start_raw) if session_start_raw else datetime.now(timezone.utc)
            if session_start_dt.tzinfo is None:
                session_start_dt = session_start_dt.replace(tzinfo=timezone.utc)
        except Exception:
            session_start_dt = datetime.now(timezone.utc)

        # Get all trades from today
        today_start = datetime.now(MT).replace(hour=0, minute=0, second=0, microsecond=0)
        trades = search_trades(acct_id, today_start)

        # Get all trades from session start for drawdown tracking
        session_trades = search_trades(acct_id, session_start_dt)
        
        # Calculate daily P&L (gross before fees)
        gross_pnl = sum(
            float(t.get("profitAndLoss") or 0)
            for t in trades
            if t.get("profitAndLoss") is not None
        )

        # Aggregate brokerage / exchange fees so we can report net performance
        fees_paid = sum(self._extract_trade_fees(t) for t in trades)

        # Net daily P&L after fees
        daily_pnl = gross_pnl - fees_paid

        # Session-level P&L for trailing drawdown
        session_gross = sum(
            float(t.get("profitAndLoss") or 0)
            for t in session_trades
            if t.get("profitAndLoss") is not None
        )
        session_fees = sum(self._extract_trade_fees(t) for t in session_trades)
        net_pnl_since_start = session_gross - session_fees

        equity_start = float(equity_record.get("session_start_equity_usd", COMBINE_ACCOUNT_SIZE_USD))
        equity_usd = equity_start + net_pnl_since_start
        equity_peak = max(float(equity_record.get("equity_peak_usd", equity_usd)), equity_usd)

        trailing_dd_used = max(0.0, equity_peak - equity_usd)
        trailing_dd_remaining = max(0.0, TRAILING_MAX_LOSS_USD - trailing_dd_used)
        trailing_dd_used_pct = trailing_dd_used / TRAILING_MAX_LOSS_USD if TRAILING_MAX_LOSS_USD else 0.0

        # Persist equity peak updates
        if equity_peak != equity_record.get("equity_peak_usd"):
            equity_record["equity_peak_usd"] = equity_peak
            self._save_equity_state()
        
        # Count wins/losses
        winning_trades = [t for t in trades if float(t.get("profitAndLoss") or 0) > 0]
        losing_trades = [t for t in trades if float(t.get("profitAndLoss") or 0) < 0]
        
        # Check consecutive losses
        sorted_trades = sorted(trades, key=lambda t: t.get("creationTimestamp", ""))
        consecutive_losses = 0
        for trade in reversed(sorted_trades):
            pnl = float(trade.get("profitAndLoss") or 0)
            if pnl < 0:
                consecutive_losses += 1
            elif pnl > 0:
                break
        
        # Get all open positions
        all_positions = search_pos(acct_id)
        open_position_count = len([p for p in all_positions if p.get("size", 0) > 0])

        risk_state = "green"
        if trailing_dd_used_pct >= DD_SOFT_75_PCT:
            risk_state = "red"
        elif trailing_dd_used_pct >= DD_SOFT_50_PCT:
            risk_state = "yellow"

        can_trade = self._can_trade(daily_pnl, consecutive_losses) and risk_state != "red"

        account_state = {
            'daily_pnl': daily_pnl,
            'gross_pnl': gross_pnl,
            'daily_fees': fees_paid,
            'realized_pnl_today_usd': daily_pnl,
            'trade_count': len(trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': len(winning_trades) / len(trades) if trades else 0,
            'consecutive_losses': consecutive_losses,
            'open_positions': open_position_count,
            'can_trade': can_trade,
            'risk_level': self._assess_account_risk(daily_pnl, consecutive_losses, open_position_count),
            'equity_usd': equity_usd,
            'equity_peak_usd': equity_peak,
            'net_pnl_since_start': net_pnl_since_start,
            'topstep_trailing_dd_used_usd': trailing_dd_used,
            'topstep_trailing_dd_remaining_usd': trailing_dd_remaining,
            'topstep_trailing_dd_used_pct': trailing_dd_used_pct,
            'topstep_risk_state': risk_state,
            'topstep_account_size_usd': equity_start,
        }

        return account_state

    def get_account_state_cached(self, acct_id: int, max_age_seconds: int = 300) -> Dict:
        """Cached account state to avoid frequent trade scans."""
        now = time.time()
        cached = self._account_state_cache.get(acct_id)
        if cached:
            ts, state = cached
            if now - ts <= max_age_seconds:
                return state

        state = self.get_account_state(acct_id)
        self._account_state_cache[acct_id] = (now, state)
        return state
    
    def _can_trade(self, daily_pnl: float, consecutive_losses: int) -> bool:
        """Determine if account is allowed to trade based on risk limits"""
        if daily_pnl <= self.max_daily_loss:
            self.logger.warning(f"Daily loss limit reached: {daily_pnl}")
            return False
        
        if daily_pnl >= self.profit_target:
            self.logger.info(f"Daily profit target reached: {daily_pnl}")
            return False
            
        if self.consecutive_loss_guard_enabled and consecutive_losses >= self.max_consecutive_losses:
            self.logger.warning(f"Max consecutive losses reached: {consecutive_losses}")
            return False
            
        return True

    def _assess_account_risk(self, daily_pnl: float, consecutive_losses: int, open_positions: int) -> str:
        """Assess overall account risk level"""
        risk_score = 0
        
        # Check proximity to daily loss limit
        if daily_pnl < 0:
            loss_percentage = abs(daily_pnl / self.max_daily_loss)
            if loss_percentage > 0.8:
                risk_score += 3
            elif loss_percentage > 0.5:
                risk_score += 2
            elif loss_percentage > 0.25:
                risk_score += 1
        
        # Check consecutive losses
        if self.consecutive_loss_guard_enabled and self.max_consecutive_losses is not None:
            if consecutive_losses >= self.max_consecutive_losses:
                risk_score += 2
            elif self.max_consecutive_losses > 1 and consecutive_losses >= self.max_consecutive_losses - 1:
                risk_score += 1
            elif consecutive_losses >= 2:
                risk_score += 1
        else:
            if consecutive_losses >= 3:
                risk_score += 2
            elif consecutive_losses >= 2:
                risk_score += 1
            
        # Check position concentration
        if open_positions > 3:
            risk_score += 2
        elif open_positions > 1:
            risk_score += 1
            
        if risk_score >= 4:
            return "high"
        elif risk_score >= 2:
            return "medium"
        else:
            return "low"
    
    def get_position_context_for_ai(self, acct_id: int, cid: str) -> Dict:
        """
        Get position context formatted for AI decision making
        """
        position_state = self.get_position_state(acct_id, cid)
        account_state = self.get_account_state(acct_id)

        topstep_context = {
            'account_size_usd': account_state.get('topstep_account_size_usd', COMBINE_ACCOUNT_SIZE_USD),
            'trailing_max_loss_usd': TRAILING_MAX_LOSS_USD,
            'trailing_loss_limit_usd': -TRAILING_MAX_LOSS_USD,
            'trailing_dd_used_usd': account_state.get('topstep_trailing_dd_used_usd'),
            'trailing_dd_remaining_usd': account_state.get('topstep_trailing_dd_remaining_usd'),
            'trailing_dd_used_pct': account_state.get('topstep_trailing_dd_used_pct'),
            'risk_state': account_state.get('topstep_risk_state'),
            'equity_usd': account_state.get('equity_usd'),
            'equity_peak_usd': account_state.get('equity_peak_usd'),
        }

        context = {
            'current_position': {
                'has_position': position_state['has_position'],
                'size': position_state['size'],
                'side': position_state['side'],
                'entry_price': position_state['entry_price'],
                'current_price': position_state.get('current_price'),
                'current_pnl': position_state['current_pnl'],
                'unrealized_pnl': position_state.get('unrealized_pnl', 0),
                'realized_pnl': position_state.get('realized_pnl', 0),
                'duration_minutes': position_state['duration_minutes'],
                'stop_count': len(position_state['stop_orders']),
                'target_count': len(position_state['limit_orders'])
            },
            'account_metrics': {
                'daily_pnl': account_state['daily_pnl'],
                'win_rate': account_state['win_rate'],
                'consecutive_losses': account_state['consecutive_losses'],
                'open_positions': account_state['open_positions'],
                'risk_level': account_state['risk_level'],
                'can_trade': account_state['can_trade'],
                'equity_usd': account_state['equity_usd'],
                'equity_peak_usd': account_state['equity_peak_usd'],
                'trailing_dd_used_usd': account_state['topstep_trailing_dd_used_usd'],
                'trailing_dd_remaining_usd': account_state['topstep_trailing_dd_remaining_usd'],
                'trailing_dd_used_pct': account_state['topstep_trailing_dd_used_pct'],
                'risk_state': account_state['topstep_risk_state'],
            },
            'risk_limits': {
                'max_daily_loss': self.max_daily_loss,
                'profit_target': self.profit_target,
                'max_consecutive_losses': self.max_consecutive_losses,
                'consecutive_loss_guard_enabled': self.consecutive_loss_guard_enabled
            },
            'topstep': topstep_context,
        }
    
        # Add specific warnings for AI consideration
        warnings = []

        if account_state['daily_pnl'] < self.max_daily_loss * 0.5:
            warnings.append("Approaching daily loss limit")

        if account_state['consecutive_losses'] >= 2:
            warnings.append(f"On {account_state['consecutive_losses']} consecutive losses")

        if account_state.get('topstep_risk_state') == "red":
            warnings.append("Topstep trailing drawdown at or above 75% - trading blocked")
        elif account_state.get('topstep_risk_state') == "yellow":
            warnings.append("Topstep trailing drawdown above 50% - trade with caution")

        if not account_state.get('can_trade', True):
            warnings.append("Trade guard active: prefer HOLD/FLAT")
        
        if position_state['has_position']:
            if position_state['duration_minutes'] > 60:
                warnings.append(f"Position open for {position_state['duration_minutes']:.0f} minutes")
            
            if position_state.get('unrealized_pnl', 0) < -50:
                warnings.append(f"Large unrealized loss: ${position_state['unrealized_pnl']:.2f}")
            elif position_state.get('unrealized_pnl', 0) > 100:
                warnings.append(f"Large unrealized profit: ${position_state['unrealized_pnl']:.2f}")
        
        # Add position suggestions for AI
        suggestions = []
        
        if position_state['has_position']:
            if position_state['duration_minutes'] > 120:
                suggestions.append("Consider closing stale position")
            
            if position_state.get('unrealized_pnl', 0) > 20 and position_state['size'] > 1:
                suggestions.append("Consider scaling out partial position")
            
            if position_state.get('unrealized_pnl', 0) < -20:
                suggestions.append("Consider cutting losses")
                
            if len(position_state['stop_orders']) == 0:
                suggestions.append("No stop loss detected - high risk")

        context['warnings'] = warnings
        context['suggestions'] = suggestions

        return context

    @staticmethod
    def _extract_trade_fees(trade: Dict) -> float:
        """Return total brokerage/clearing fees for a trade record."""
        if not isinstance(trade, dict):
            return 0.0

        def _to_float(value) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        preferred_keys = [
            'commissionAndFees',
            'totalFees',
            'brokerageFeesTotal',
            'feesTotal',
        ]
        for key in preferred_keys:
            if key in trade:
                fee_value = _to_float(trade.get(key))
                if fee_value:
                    return abs(fee_value)

        fee_sum = 0.0
        for key, value in trade.items():
            if not isinstance(key, str):
                continue
            lower_key = key.lower()
            if 'fee' in lower_key or 'commission' in lower_key:
                fee_sum += abs(_to_float(value))

        return fee_sum
