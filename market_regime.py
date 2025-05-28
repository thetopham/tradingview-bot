# market_regime.py - SIMPLIFIED VERSION
import logging
from datetime import datetime
from typing import Dict, List, Optional
import statistics

class MarketRegime:
    """
    Simplified market regime detection for day trading
    Focus on: trending, ranging, or choppy
    """
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
    def analyze_regime(self, timeframe_data: Dict[str, Dict]) -> Dict:
        """
        Simple regime analysis based on price action
        
        Args:
            timeframe_data: Dict with '5m', '15m', '30m' data
            
        Returns:
            Dict with regime, confidence, and trade recommendation
        """
        try:
            # Extract simple metrics
            metrics = self._extract_metrics(timeframe_data)
            
            if not metrics:
                return self._default_response()
            
            # Simple regime determination
            regime, confidence = self._determine_regime(metrics)
            
            # Simple trade recommendation
            can_trade = confidence > 60 and regime != 'choppy'
            
            return {
                'regime': regime,
                'confidence': confidence,
                'can_trade': can_trade,
                'timeframes_aligned': self._check_alignment(metrics),
                'timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            self.logger.error(f"Regime analysis error: {e}")
            return self._default_response()
    
    def _extract_metrics(self, data: Dict) -> Dict:
        """Extract only essential metrics"""
        metrics = {}
        
        for tf in ['5m', '15m', '30m']:
            if tf in data and isinstance(data[tf], dict):
                tf_data = data[tf]
                
                # Get trend direction from signal or trend field
                signal = tf_data.get('signal', 'HOLD')
                trend = tf_data.get('trend', tf_data.get('trend_direction', 'sideways'))
                
                # Normalize trend
                if signal == 'BUY' or trend in ['up', 'bullish']:
                    direction = 'up'
                elif signal == 'SELL' or trend in ['down', 'bearish']:
                    direction = 'down'
                else:
                    direction = 'sideways'
                
                metrics[tf] = {
                    'direction': direction,
                    'momentum': tf_data.get('momentum', 'neutral'),
                    'volatility': tf_data.get('volatility', 'medium')
                }
                
        return metrics
    
    def _determine_regime(self, metrics: Dict) -> tuple:
        """Simple regime determination"""
        
        # Count directions
        directions = [m['direction'] for m in metrics.values()]
        up_count = directions.count('up')
        down_count = directions.count('down')
        sideways_count = directions.count('sideways')
        total = len(directions)
        
        # Simple logic:
        # - If 2+ timeframes agree on direction = trending
        # - If all sideways = ranging
        # - Otherwise = choppy
        
        if up_count >= 2:
            regime = 'trending_up'
            confidence = int((up_count / total) * 100)
        elif down_count >= 2:
            regime = 'trending_down'
            confidence = int((down_count / total) * 100)
        elif sideways_count >= 2:
            regime = 'ranging'
            confidence = 70  # Fixed confidence for ranging
        else:
            regime = 'choppy'
            confidence = 50  # Low confidence for choppy
            
        return regime, confidence
    
    def _check_alignment(self, metrics: Dict) -> bool:
        """Check if 15m and 30m align (most important for day trading)"""
        if '15m' in metrics and '30m' in metrics:
            return metrics['15m']['direction'] == metrics['30m']['direction']
        return False
    
    def _default_response(self) -> Dict:
        """Default safe response"""
        return {
            'regime': 'choppy',
            'confidence': 0,
            'can_trade': False,
            'timeframes_aligned': False,
            'timestamp': datetime.now().isoformat()
        }
