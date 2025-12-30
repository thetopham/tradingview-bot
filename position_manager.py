# position_manager.py
"""
Simplified position context provider for AI trading decisions
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from api import search_pos, search_open, search_trades, get_contract
from config import load_config

config = load_config()
CT = config['CT']

class PositionManager:
    """
    Provides position and account context for AI decisions - no autonomous actions
    """
    
    def __init__(self, accounts: Dict[str, int]):
        self.accounts = accounts
        self.logger = logging.getLogger(__name__)
        
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
        # Get all trades from today
        today_start = datetime.now(CT).replace(hour=0, minute=0, second=0, microsecond=0)
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
            'risk_level': self._assess_account_risk(daily_pnl, consecutive_losses, open_position_count)
        }
    
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
                'can_trade': account_state['can_trade']
            },
            'risk_limits': {
                'max_daily_loss': self.max_daily_loss,
                'profit_target': self.profit_target,
                'max_consecutive_losses': self.max_consecutive_losses,
                'consecutive_loss_guard_enabled': self.consecutive_loss_guard_enabled
            }
        }
    
        # Add specific warnings for AI consideration
        warnings = []
        
        if account_state['daily_pnl'] < self.max_daily_loss * 0.5:
            warnings.append("Approaching daily loss limit")
    
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
