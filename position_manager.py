# position_manager.py
"""
Simplified position context provider for AI trading decisions
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from api import search_pos, search_open, search_trades, get_contract
from config import load_config

config = load_config()
MT = config['MT']

class PositionManager:
    """
    Provides position and account context for AI decisions - no autonomous actions
    """
    
    def __init__(self, accounts: Dict[str, int]):
        self.accounts = accounts
        self.logger = logging.getLogger(__name__)
        self._account_state_cache: Dict[int, Tuple[float, Dict]] = {}
        self._equity_state_path = Path("./account_equity_state.json")
        self._equity_state = self._load_equity_state()

        # Trailing drawdown parameters
        self.trailing_max_loss_usd = 2000.0
        self.trailing_loss_limit_usd = -self.trailing_max_loss_usd
        self.trail_guard_floor_usd = 48000.0
        self.trail_activation_threshold_usd = 50000.0
        
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

    # ------------------------------------------------------------------
    # Equity state persistence
    # ------------------------------------------------------------------
    def _load_equity_state(self) -> Dict[str, Dict]:
        if not self._equity_state_path.exists():
            return {}

        try:
            with self._equity_state_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as exc:  # noqa: BLE001 - tolerant restore
            self.logger.warning("Failed to load equity state from %s: %s", self._equity_state_path, exc)
        return {}

    def _save_equity_state(self) -> None:
        try:
            self._equity_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._equity_state_path.with_suffix(self._equity_state_path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(self._equity_state, f, ensure_ascii=False)
            tmp_path.replace(self._equity_state_path)
        except Exception as exc:  # noqa: BLE001 - log-only persistence errors
            self.logger.error("Failed to persist equity state to %s: %s", self._equity_state_path, exc)

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
        overview = self._get_account_overview(acct_id)
        account_balance = overview.get("balance")
        trailing_state = self._compute_trailing_drawdown(acct_id, account_balance)

        # Get all trades from today
        today_start = datetime.now(MT).replace(hour=0, minute=0, second=0, microsecond=0)
        trades = search_trades(acct_id, today_start)
        
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

        trailing_can_trade = not trailing_state.get("below_balance_floor") and not trailing_state.get(
            "trailing_violation"
        )
        broker_can_trade = overview.get("canTrade") if isinstance(overview, dict) else None
        combined_can_trade = trailing_can_trade and self._can_trade(daily_pnl, consecutive_losses)
        if broker_can_trade is False:
            combined_can_trade = False

        return {
            'daily_pnl': daily_pnl,
            'gross_pnl': gross_pnl,
            'daily_fees': fees_paid,
            'account_balance_usd': account_balance,
            'realized_pnl_today_usd': gross_pnl,
            'trade_count': len(trades),
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': len(winning_trades) / len(trades) if trades else 0,
            'consecutive_losses': consecutive_losses,
            'open_positions': open_position_count,
            'can_trade': combined_can_trade,
            'broker_can_trade': broker_can_trade,
            'risk_level': self._assess_account_risk(daily_pnl, consecutive_losses, open_position_count),
            'trailing_drawdown': trailing_state,
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

    # ------------------------------------------------------------------
    # Account balance + trailing drawdown helpers
    # ------------------------------------------------------------------
    def _get_current_account_balance(self, acct_id: int) -> Optional[float]:
        try:
            from api import get_account_balance

            balance = get_account_balance(acct_id)
            if balance is None:
                return None
            return float(balance)
        except Exception as exc:  # noqa: BLE001 - external call should never raise
            self.logger.warning("Account balance lookup failed for %s: %s", acct_id, exc)
            return None

    def _get_account_overview(self, acct_id: int) -> Dict[str, Optional[float]]:
        """Return broker account flags + balance when available."""

        try:
            from api import get_account_overview

            overview = get_account_overview(acct_id) or {}
        except Exception as exc:  # noqa: BLE001 - external call should never raise
            self.logger.warning("Account overview lookup failed for %s: %s", acct_id, exc)
            overview = {}

        balance = overview.get("balance") if isinstance(overview, dict) else None
        if balance is None:
            balance = self._get_current_account_balance(acct_id)

        return {
            "balance": balance,
            "canTrade": overview.get("canTrade") if isinstance(overview, dict) else None,
            "isVisible": overview.get("isVisible") if isinstance(overview, dict) else None,
            "name": overview.get("name") if isinstance(overview, dict) else None,
        }

    def _update_equity_tracking(self, acct_id: int, balance: Optional[float]) -> Dict[str, Optional[float]]:
        key = str(acct_id)
        record = self._equity_state.setdefault(
            key,
            {
                "session_start_equity_usd": None,
                "equity_peak_usd": None,
                "last_balance_usd": None,
                "last_balance_ts": None,
            },
        )

        now_ts = time.time()
        if balance is not None:
            record["last_balance_usd"] = balance
            record["last_balance_ts"] = now_ts
            if record.get("session_start_equity_usd") is None:
                record["session_start_equity_usd"] = balance
            if record.get("equity_peak_usd") is None:
                record["equity_peak_usd"] = balance
            else:
                record["equity_peak_usd"] = max(record["equity_peak_usd"], balance)
            self._save_equity_state()
        elif record.get("session_start_equity_usd") is None:
            # Provide a conservative default if we have no history at all
            record["session_start_equity_usd"] = self.trail_activation_threshold_usd
            record["equity_peak_usd"] = self.trail_activation_threshold_usd

        return record

    def _compute_trailing_drawdown(self, acct_id: int, balance: Optional[float]) -> Dict[str, Optional[float]]:
        record = self._update_equity_tracking(acct_id, balance)

        current_equity = (
            balance
            if balance is not None
            else record.get("last_balance_usd") or record.get("session_start_equity_usd")
        )
        session_start = record.get("session_start_equity_usd")
        equity_peak = record.get("equity_peak_usd") or current_equity

        if equity_peak is not None and self.trail_activation_threshold_usd:
            equity_peak = max(equity_peak, self.trail_activation_threshold_usd)

        trailing_dd_used_usd = 0.0
        if equity_peak is not None and current_equity is not None:
            trailing_dd_used_usd = max(0.0, equity_peak - current_equity)

        trailing_dd_remaining_usd = max(0.0, self.trailing_max_loss_usd - trailing_dd_used_usd)
        trailing_dd_used_pct = (
            trailing_dd_used_usd / self.trailing_max_loss_usd if self.trailing_max_loss_usd else 0.0
        )

        if trailing_dd_used_pct < 0.50:
            risk_state = "green"
        elif trailing_dd_used_pct < 0.75:
            risk_state = "yellow"
        else:
            risk_state = "red"

        below_floor = current_equity is not None and current_equity < self.trail_guard_floor_usd
        trailing_violation = trailing_dd_used_usd >= self.trailing_max_loss_usd

        return {
            "account_balance_usd": current_equity,
            "session_start_equity_usd": session_start,
            "equity_peak_usd": equity_peak,
            "trailing_max_loss_usd": self.trailing_max_loss_usd,
            "trailing_loss_limit_usd": self.trailing_loss_limit_usd,
            "trailing_dd_used_usd": trailing_dd_used_usd,
            "trailing_dd_remaining_usd": trailing_dd_remaining_usd,
            "trailing_dd_used_pct": trailing_dd_used_pct,
            "risk_state": risk_state,
            "below_balance_floor": below_floor,
            "trailing_violation": trailing_violation,
        }
    
    def get_position_context_for_ai(self, acct_id: int, cid: str) -> Dict:
        """
        Get position context formatted for AI decision making
        """
        position_state = self.get_position_state(acct_id, cid)
        account_state = self.get_account_state(acct_id)
        trailing = account_state.get('trailing_drawdown', {}) or {}

        topstep_can_trade = not trailing.get('trailing_violation') and not trailing.get('below_balance_floor')
        if account_state.get('broker_can_trade') is False:
            topstep_can_trade = False

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
                'realized_pnl_today_usd': account_state.get('realized_pnl_today_usd'),
                'account_balance_usd': account_state.get('account_balance_usd'),
                'win_rate': account_state['win_rate'],
                'consecutive_losses': account_state['consecutive_losses'],
                'open_positions': account_state['open_positions'],
                'risk_level': account_state['risk_level'],
                'can_trade': account_state['can_trade'],
                'broker_can_trade': account_state.get('broker_can_trade'),
                'trailing_dd_used_usd': trailing.get('trailing_dd_used_usd'),
                'trailing_dd_remaining_usd': trailing.get('trailing_dd_remaining_usd'),
                'trailing_dd_used_pct': trailing.get('trailing_dd_used_pct'),
                'trailing_risk_state': trailing.get('risk_state'),
                'trailing_can_trade': topstep_can_trade,
            },
            'risk_limits': {
                'max_daily_loss': self.max_daily_loss,
                'profit_target': self.profit_target,
                'max_consecutive_losses': self.max_consecutive_losses,
                'consecutive_loss_guard_enabled': self.consecutive_loss_guard_enabled,
                'trailing_max_loss_usd': self.trailing_max_loss_usd,
                'trailing_loss_limit_usd': self.trailing_loss_limit_usd,
            },
            'topstep': {
                'account_size_usd': trailing.get('account_balance_usd'),
                'account_balance_usd': trailing.get('account_balance_usd'),
                'session_start_equity_usd': trailing.get('session_start_equity_usd'),
                'equity_peak_usd': trailing.get('equity_peak_usd'),
                'trailing_max_loss_usd': self.trailing_max_loss_usd,
                'trailing_loss_limit_usd': self.trailing_loss_limit_usd,
                'trailing_dd_used_usd': trailing.get('trailing_dd_used_usd'),
                'trailing_dd_remaining_usd': trailing.get('trailing_dd_remaining_usd'),
                'trailing_dd_used_pct': trailing.get('trailing_dd_used_pct'),
                'risk_state': trailing.get('risk_state'),
                'broker_can_trade': account_state.get('broker_can_trade'),
                'dd_soft_50_pct': 0.50,
                'dd_soft_75_pct': 0.75,
                'below_balance_floor': trailing.get('below_balance_floor'),
                'trailing_violation': trailing.get('trailing_violation'),
            }
        }
    
        # Add specific warnings for AI consideration
        warnings = []

        if account_state['daily_pnl'] < self.max_daily_loss * 0.5:
            warnings.append("Approaching daily loss limit")

        if not account_state['can_trade']:
            warnings.append("Trading disabled by risk guard (daily or trailing)")

        if account_state.get('broker_can_trade') is False:
            warnings.append("Broker indicates trading disabled (canTrade=false)")

        if trailing.get('risk_state') == 'red':
            warnings.append("Topstep trailing drawdown RED - bias HOLD/FLAT")

        if trailing.get('below_balance_floor'):
            warnings.append("Account balance below $48k guardrail")

        if account_state['consecutive_losses'] >= 2:
            warnings.append(f"On {account_state['consecutive_losses']} consecutive losses")
        
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

        if trailing.get('risk_state') == 'red' or not topstep_can_trade:
            suggestions.append("Bias HOLD/FLAT due to trailing drawdown guard or broker canTrade=false")

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
