# position_manager.py
"""
Simplified position context provider for AI trading decisions
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
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

TOPSTEP_STATE_PATH = Path(os.environ.get("TOPSTEP_STATE_PATH", "./topstep_state.json"))
TOPSTEP_STATE_BAK = TOPSTEP_STATE_PATH.with_suffix(TOPSTEP_STATE_PATH.suffix + ".bak")

class PositionManager:
    """
    Provides position and account context for AI decisions - no autonomous actions
    """
    
    def __init__(self, accounts: Dict[str, int]):
        self.accounts = accounts
        self.logger = logging.getLogger(__name__)
        self._account_state_cache: Dict[int, Tuple[float, Dict]] = {}

        self._topstep_state_lock = RLock()
        self._topstep_state: Dict[str, Dict[str, float]] = {}
        self._last_topstep_save = 0.0
        self._load_topstep_state()
        
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

    def _load_topstep_state(self) -> None:
        with self._topstep_state_lock:
            state_data = None
            primary_error = None

            def _load_from(path: Path):
                with path.open("r", encoding="utf-8") as f:
                    return json.load(f)

            try:
                if TOPSTEP_STATE_PATH.exists():
                    state_data = _load_from(TOPSTEP_STATE_PATH)
            except Exception as exc:
                primary_error = exc
                self.logger.warning("Failed to parse topstep state from %s: %s", TOPSTEP_STATE_PATH, exc)

            if state_data is None and TOPSTEP_STATE_BAK.exists():
                try:
                    state_data = _load_from(TOPSTEP_STATE_BAK)
                    self.logger.warning(
                        "Recovered topstep state from backup %s after parse failure", TOPSTEP_STATE_BAK
                    )
                except Exception as exc:
                    self.logger.warning("Failed to parse backup topstep state from %s: %s", TOPSTEP_STATE_BAK, exc)

            if state_data is None:
                if primary_error:
                    self.logger.warning(
                        "Starting with empty topstep state due to parse errors; primary=%s", primary_error
                    )
                self._topstep_state = {}
                self._last_topstep_save = 0.0
                return

            self._topstep_state = state_data or {}
            self._last_topstep_save = 0.0
            self.logger.info("Loaded topstep state for %s accounts", len(self._topstep_state))

    def _save_topstep_state(self, force: bool = False) -> None:
        with self._topstep_state_lock:
            now = time.time()
            if not force and now - self._last_topstep_save < 10:
                return

            data = self._topstep_state
            try:
                if TOPSTEP_STATE_BAK.exists():
                    TOPSTEP_STATE_BAK.unlink()
            except Exception:
                pass
            try:
                if TOPSTEP_STATE_PATH.exists():
                    TOPSTEP_STATE_PATH.rename(TOPSTEP_STATE_BAK)
            except Exception:
                pass

            try:
                TOPSTEP_STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
                self._last_topstep_save = now
            except Exception as exc:
                self.logger.error("Failed to save topstep state to %s: %s", TOPSTEP_STATE_PATH, exc)

    def _get_topstep_account_state(self, acct_id: int) -> Dict[str, float]:
        acct_key = str(acct_id)
        with self._topstep_state_lock:
            state = self._topstep_state.get(acct_key)
            if not state:
                state = {
                    "session_start_equity_usd": float(COMBINE_ACCOUNT_SIZE_USD),
                    "equity_peak_usd": float(COMBINE_ACCOUNT_SIZE_USD),
                    "session_start_ts": datetime.now(MT).isoformat(),
                }
                self._topstep_state[acct_key] = state
                self._save_topstep_state(force=True)
            return state

    def _update_topstep_account_state(self, acct_id: int, *, equity_peak_usd: Optional[float] = None) -> None:
        acct_key = str(acct_id)
        with self._topstep_state_lock:
            state = self._topstep_state.get(acct_key)
            if not state:
                return
            changed = False
            if equity_peak_usd is not None and equity_peak_usd > state.get("equity_peak_usd", 0):
                state["equity_peak_usd"] = equity_peak_usd
                changed = True
            if changed:
                self._save_topstep_state()

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
        ts_state = self._get_topstep_account_state(acct_id)
        try:
            session_start_dt = parser.isoparse(str(ts_state.get("session_start_ts"))).astimezone(MT)
        except Exception:
            session_start_dt = datetime.now(MT).replace(microsecond=0)

        # Get all trades from today
        today_start = datetime.now(MT).replace(hour=0, minute=0, second=0, microsecond=0)
        trades = search_trades(acct_id, today_start)
        trades_since_session = search_trades(acct_id, session_start_dt)
        
        # Calculate daily P&L (gross before fees)
        gross_pnl = sum(
            float(t.get("profitAndLoss") or 0)
            for t in trades
            if t.get("profitAndLoss") is not None
        )

        # Aggregate brokerage / exchange fees so we can report net performance
        fees_paid = sum(self._extract_trade_fees(t) for t in trades)
        session_fees_paid = sum(self._extract_trade_fees(t) for t in trades_since_session)

        # Net daily P&L after fees
        daily_pnl = gross_pnl - fees_paid
        realized_pnl_today = daily_pnl
        session_realized_pnl = (
            sum(float(t.get("profitAndLoss") or 0) for t in trades_since_session if t.get("profitAndLoss") is not None)
            - session_fees_paid
        )
        
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

        equity_usd = float(ts_state.get("session_start_equity_usd", COMBINE_ACCOUNT_SIZE_USD)) + session_realized_pnl
        equity_peak_usd = max(float(ts_state.get("equity_peak_usd", COMBINE_ACCOUNT_SIZE_USD)), equity_usd)
        self._update_topstep_account_state(acct_id, equity_peak_usd=equity_peak_usd)

        trailing_dd_used_usd = max(0.0, equity_peak_usd - equity_usd)
        trailing_dd_remaining_usd = max(0.0, TRAILING_MAX_LOSS_USD - trailing_dd_used_usd)
        trailing_dd_used_pct = trailing_dd_used_usd / TRAILING_MAX_LOSS_USD if TRAILING_MAX_LOSS_USD else 0.0

        if trailing_dd_used_pct < DD_SOFT_50_PCT:
            topstep_risk_state = "green"
        elif trailing_dd_used_pct < DD_SOFT_75_PCT:
            topstep_risk_state = "yellow"
        else:
            topstep_risk_state = "red"

        topstep_context = {
            "account_size_usd": float(ts_state.get("session_start_equity_usd", COMBINE_ACCOUNT_SIZE_USD)),
            "trailing_max_loss_usd": TRAILING_MAX_LOSS_USD,
            "trailing_loss_limit_usd": -float(TRAILING_MAX_LOSS_USD),
            "trailing_dd_used_usd": trailing_dd_used_usd,
            "trailing_dd_remaining_usd": trailing_dd_remaining_usd,
            "trailing_dd_used_pct": trailing_dd_used_pct,
            "risk_state": topstep_risk_state,
            "equity_usd": equity_usd,
            "equity_peak_usd": equity_peak_usd,
            "realized_pnl_today_usd": realized_pnl_today,
        }
        
        return {
            'daily_pnl': daily_pnl,
            'gross_pnl': gross_pnl,
            'daily_fees': fees_paid,
            'trade_count': len(trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': len(winning_trades) / len(trades) if trades else 0,
            'consecutive_losses': consecutive_losses,
            'open_positions': open_position_count,
            'can_trade': self._can_trade(daily_pnl, consecutive_losses),
            'risk_level': self._assess_account_risk(daily_pnl, consecutive_losses, open_position_count),
            'topstep': topstep_context,
        }

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
            },
            'risk_limits': {
                'max_daily_loss': self.max_daily_loss,
                'profit_target': self.profit_target,
                'max_consecutive_losses': self.max_consecutive_losses,
                'consecutive_loss_guard_enabled': self.consecutive_loss_guard_enabled
            },
            'topstep': account_state.get('topstep', {}),
        }
    
        # Add specific warnings for AI consideration
        warnings = []
        
        if account_state['daily_pnl'] < self.max_daily_loss * 0.5:
            warnings.append("Approaching daily loss limit")

        if account_state['consecutive_losses'] >= 2:
            warnings.append(f"On {account_state['consecutive_losses']} consecutive losses")

        topstep_ctx = account_state.get('topstep', {}) or {}
        risk_state = topstep_ctx.get('risk_state')
        if risk_state == "red":
            warnings.append("Topstep trailing drawdown in RED zone – prefer HOLD/FLAT")
        elif risk_state == "yellow":
            warnings.append("Topstep trailing drawdown in caution (YELLOW) zone")
        if not account_state.get('can_trade', True):
            warnings.append("Trading disabled by risk guard")

        if position_state['has_position']:
            if position_state['duration_minutes'] > 60:
                warnings.append(f"Position open for {position_state['duration_minutes']:.0f} minutes")
            
            if position_state.get('unrealized_pnl', 0) < -50:
                warnings.append(f"Large unrealized loss: ${position_state['unrealized_pnl']:.2f}")
            elif position_state.get('unrealized_pnl', 0) > 100:
                warnings.append(f"Large unrealized profit: ${position_state['unrealized_pnl']:.2f}")
        
        # Add position suggestions for AI
        suggestions = []

        if topstep_ctx.get('risk_state') == "red" or not account_state.get('can_trade', True):
            suggestions.append("Risk controls: prefer HOLD/FLAT (Topstep or guard active)")

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
