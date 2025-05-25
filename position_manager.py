# position_manager.py
"""
Position management module for autonomous trading decisions and position adjustments
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import statistics

from api import (
    search_pos, search_open, search_trades, place_stop, place_limit,
    cancel, get_contract, flatten_contract, place_market,
    ai_trade_decision_with_regime, get_market_conditions_summary
)
from config import load_config
from market_regime import MarketRegime

config = load_config()
CT = config['CT']

class PositionManager:
    """
    Manages trading positions autonomously based on market conditions and P&L
    """
    
    def __init__(self, accounts: Dict[str, int]):
        self.accounts = accounts
        self.logger = logging.getLogger(__name__)
        self.market_regime = MarketRegime()
        
        # Risk parameters
        self.max_daily_loss = config.get('MAX_DAILY_LOSS', -500.0)
        self.profit_target = config.get('DAILY_PROFIT_TARGET', 500.0)
        self.max_consecutive_losses = config.get('MAX_CONSECUTIVE_LOSSES', 3)
        self.trailing_stop_activation = config.get('TRAILING_STOP_ACTIVATION', 10.0)  # points
        self.trailing_stop_distance = config.get('TRAILING_STOP_DISTANCE', 5.0)  # points
        
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
                'stop_orders': [],
                'limit_orders': [],
                'duration_minutes': 0
            }
        
        # Calculate aggregate position
        total_size = sum(p.get("size", 0) for p in positions)
        avg_price = sum(p.get("averagePrice", 0) * p.get("size", 0) for p in positions) / total_size if total_size > 0 else 0
        
        # Determine side (1 = long, 2 = short)
        position_type = positions[0].get("type") if positions else None
        side = "LONG" if position_type == 1 else "SHORT" if position_type == 2 else None
        
        # Get position age
        creation_time = positions[0].get("creationTimestamp")
        if creation_time:
            try:
                from dateutil import parser
                entry_time = parser.parse(creation_time)
                duration = (datetime.now(datetime.timezone.utc) - entry_time).total_seconds() / 60
            except:
                duration = 0
        else:
            duration = 0
        
        # Categorize orders
        stop_orders = [o for o in open_orders if o["type"] == 4 and o["status"] == 1]
        limit_orders = [o for o in open_orders if o["type"] == 1 and o["status"] == 1]
        
        # Get recent trades for P&L calculation
        trades = search_trades(acct_id, datetime.now(CT) - timedelta(hours=24))
        position_trades = [t for t in trades if t["contractId"] == cid and not t.get("voided", False)]
        
        # Calculate unrealized P&L (would need market price - not available in current setup)
        # For now, use realized P&L from partial fills
        current_pnl = sum(float(t.get("profitAndLoss") or 0) for t in position_trades if t.get("profitAndLoss") is not None)
        
        return {
            'has_position': True,
            'size': total_size,
            'side': side,
            'entry_price': avg_price,
            'current_pnl': current_pnl,
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
        
        # Calculate daily P&L
        daily_pnl = sum(float(t.get("profitAndLoss") or 0) for t in trades if t.get("profitAndLoss") is not None)
        
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
            
        if consecutive_losses >= self.max_consecutive_losses:
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
        if consecutive_losses >= self.max_consecutive_losses - 1:
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
    
    def manage_position(self, acct_id: int, cid: str, position_state: Dict) -> Dict:
        """
        Actively manage an open position - adjust stops, scale out, etc.
        Returns action taken
        """
        if not position_state['has_position']:
            return {'action': 'none', 'reason': 'No position to manage'}
        
        actions_taken = []
        
        # 1. Check if we should trail stop
        if position_state['current_pnl'] >= self.trailing_stop_activation:
            result = self._adjust_trailing_stop(acct_id, cid, position_state)
            if result['adjusted']:
                actions_taken.append(result)
        
        # 2. Check if we should scale out on profit
        if position_state['current_pnl'] >= 20 and position_state['size'] > 1:
            result = self._scale_out_profit(acct_id, cid, position_state)
            if result['scaled']:
                actions_taken.append(result)
        
        # 3. Check if position is stale and should be closed
        if position_state['duration_minutes'] > 120:  # 2 hours
            market_conditions = get_market_conditions_summary()
            if market_conditions['regime'] == 'choppy':
                self.logger.info(f"Closing stale position in choppy market")
                flatten_contract(acct_id, cid, timeout=10)
                return {'action': 'flatten', 'reason': 'Stale position in choppy market'}
        
        # 4. Check if we should add to winning position
        if (position_state['current_pnl'] >= 10 and 
            position_state['size'] < 3 and 
            position_state['duration_minutes'] < 30):
            result = self._consider_adding_to_position(acct_id, cid, position_state)
            if result['added']:
                actions_taken.append(result)
        
        return {
            'action': 'managed',
            'actions_taken': actions_taken
        }
    
    def _adjust_trailing_stop(self, acct_id: int, cid: str, position_state: Dict) -> Dict:
        """Adjust stop loss to trail price"""
        current_stops = position_state['stop_orders']
        if not current_stops:
            return {'adjusted': False, 'reason': 'No stop orders found'}
        
        # Cancel existing stops and place new ones
        for stop in current_stops:
            cancel(acct_id, stop['id'])
        
        # Calculate new stop price
        if position_state['side'] == 'LONG':
            new_stop_price = position_state['entry_price'] + self.trailing_stop_distance
        else:
            new_stop_price = position_state['entry_price'] - self.trailing_stop_distance
            
        # Place new stop
        stop_side = 1 if position_state['side'] == 'LONG' else 0
        place_stop(acct_id, cid, stop_side, position_state['size'], new_stop_price)
        
        self.logger.info(f"Adjusted trailing stop to {new_stop_price}")
        return {
            'adjusted': True,
            'new_stop': new_stop_price,
            'reason': 'Trailing stop adjusted'
        }
    
    def _scale_out_profit(self, acct_id: int, cid: str, position_state: Dict) -> Dict:
        """Scale out partial position on profit"""
        scale_size = max(1, position_state['size'] // 3)
        
        # Place market order to scale out
        exit_side = 1 if position_state['side'] == 'LONG' else 0
        place_market(acct_id, cid, exit_side, scale_size)
        
        self.logger.info(f"Scaled out {scale_size} contracts at profit")
        return {
            'scaled': True,
            'size': scale_size,
            'reason': f'Scaled out at {position_state["current_pnl"]} profit'
        }
    
    def _consider_adding_to_position(self, acct_id: int, cid: str, position_state: Dict) -> Dict:
        """Consider adding to a winning position"""
        # Check market conditions first
        market_conditions = get_market_conditions_summary()
        
        if not market_conditions['trade_recommended']:
            return {'added': False, 'reason': 'Market conditions not favorable'}
        
        if market_conditions['regime'] not in ['trending_up', 'trending_down']:
            return {'added': False, 'reason': 'Not in trending market'}
        
        # Add 1 contract
        side = 0 if position_state['side'] == 'LONG' else 1
        place_market(acct_id, cid, side, 1)
        
        # Add protective stop
        stop_side = 1 - side
        stop_price = (position_state['entry_price'] - 5.0 if side == 0 
                     else position_state['entry_price'] + 5.0)
        place_stop(acct_id, cid, stop_side, 1, stop_price)
        
        self.logger.info(f"Added to position in {market_conditions['regime']} market")
        return {
            'added': True,
            'size': 1,
            'reason': 'Added to winning position in trending market'
        }
    
    def scan_for_opportunities(self, acct_id: int, account_name: str) -> Optional[Dict]:
        """
        Scan market conditions and initiate new trades autonomously
        Returns trade decision or None
        """
        # Check if account can trade
        account_state = self.get_account_state(acct_id)
        if not account_state['can_trade']:
            self.logger.info(f"Account {account_name} cannot trade: risk limits")
            return None
        
        # Check if we already have positions
        if account_state['open_positions'] >= 2:
            self.logger.info(f"Account {account_name} has max positions")
            return None
        
        # Get market conditions
        market_summary = get_market_conditions_summary()
        
        # Only trade in strong regimes
        if market_summary['confidence'] < 70:
            return None
            
        if market_summary['regime'] not in ['trending_up', 'trending_down', 'breakout']:
            return None
            
        if not market_summary['trade_recommended']:
            return None
        
        # Determine signal based on regime
        if market_summary['regime'] == 'trending_up':
            signal = 'BUY'
        elif market_summary['regime'] == 'trending_down':
            signal = 'SELL'
        else:  # breakout
            # Would need additional logic to determine breakout direction
            return None
        
        # Adjust size based on account risk
        base_size = 2
        if account_state['risk_level'] == 'high':
            size = 1
        elif account_state['risk_level'] == 'medium':
            size = 1
        else:
            size = base_size
        
        # Build autonomous trade decision
        trade_decision = {
            'account': account_name,
            'strategy': 'bracket',
            'signal': signal,
            'symbol': 'CON.F.US.MES.M25',
            'size': size,
            'alert': f"Autonomous: {market_summary['regime']} regime",
            'reason': f"High confidence {market_summary['regime']} detected",
            'autonomous': True,
            'market_conditions': market_summary
        }
        
        self.logger.info(f"Autonomous trade opportunity: {trade_decision}")
        return trade_decision
    
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
                'current_pnl': position_state['current_pnl'],
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
                'max_consecutive_losses': self.max_consecutive_losses
            }
        }
        
        # Add specific warnings
        warnings = []
        if account_state['daily_pnl'] < self.max_daily_loss * 0.5:
            warnings.append("Approaching daily loss limit")
        
        if account_state['consecutive_losses'] >= 2:
            warnings.append(f"On {account_state['consecutive_losses']} consecutive losses")
            
        if position_state['has_position'] and position_state['duration_minutes'] > 60:
            warnings.append("Position open for over 1 hour")
            
        context['warnings'] = warnings
        
        return context
