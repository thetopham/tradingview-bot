# market_regime.py
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import statistics

class MarketRegime:
    """
    Market regime detection based on multiple timeframes and indicators
    """
    
    REGIME_TRENDING_UP = "trending_up"
    REGIME_TRENDING_DOWN = "trending_down"
    REGIME_RANGING = "ranging"
    REGIME_CHOPPY = "choppy"
    REGIME_BREAKOUT = "breakout"
    REGIME_REVERSAL = "reversal"
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
    def analyze_regime(self, timeframe_data: Dict[str, Dict]) -> Dict:
        """
        Analyze market regime based on multiple timeframe data
        
        Args:
            timeframe_data: Dict with keys like '1m', '5m', '15m', '1h' containing chart analysis
            
        Returns:
            Dict with regime analysis including:
            - primary_regime: Main market regime
            - confidence: Confidence level (0-100)
            - supporting_factors: List of supporting evidence
            - trade_recommendation: Whether to trade in this regime
            - risk_level: Risk assessment (low/medium/high)
        """
        try:
            # Extract key metrics from each timeframe
            metrics = self._extract_metrics(timeframe_data)
            
            # Analyze trend alignment across timeframes
            trend_analysis = self._analyze_trend_alignment(metrics)
            
            # Analyze volatility and range
            volatility_analysis = self._analyze_volatility(metrics)
            
            # Analyze momentum
            momentum_analysis = self._analyze_momentum(metrics)
            
            # Determine primary regime
            regime_result = self._determine_regime(
                trend_analysis, 
                volatility_analysis, 
                momentum_analysis,
                metrics
            )
            
            # Add trading recommendations based on regime
            regime_result['trade_recommendation'] = self._get_trade_recommendation(regime_result)
            regime_result['risk_level'] = self._assess_risk_level(regime_result, metrics)
            
            return regime_result
            
        except Exception as e:
            self.logger.error(f"Error in regime analysis: {e}")
            return {
                'primary_regime': self.REGIME_CHOPPY,
                'confidence': 0,
                'supporting_factors': ['Error in analysis'],
                'trade_recommendation': False,
                'risk_level': 'high'
            }
    
    def _extract_metrics(self, timeframe_data: Dict[str, Dict]) -> Dict:
        """Extract key metrics from timeframe data"""
        metrics = {}
        
        for tf, data in timeframe_data.items():
            if not data:
                continue
                
            metrics[tf] = {
                'trend': data.get('trend', 'unknown'),
                'momentum': data.get('momentum', 'neutral'),
                'volatility': data.get('volatility', 'medium'),
                'signal': data.get('signal', 'HOLD'),
                'support': data.get('support', []),
                'resistance': data.get('resistance', []),
                'current_price': data.get('current_price', 0),
                'range_size': data.get('range_size', 0),
                'indicators': data.get('indicators', {}),
                'volume_trend': data.get('volume_trend', 'flat')
            }
            
        return metrics
    
    def _analyze_trend_alignment(self, metrics: Dict) -> Dict:
        """Analyze trend alignment across timeframes"""
        trends = []
        weighted_trends = {'up': 0, 'down': 0, 'sideways': 0}
        
        # Weight higher timeframes more
        weights = {
            '1m': 0.1,
            '5m': 0.15,
            '15m': 0.25,
            '30m': 0.25,
            '1h': 0.25
        }
        
        for tf, weight in weights.items():
            if tf in metrics:
                trend = metrics[tf].get('trend', 'sideways')
                if trend in weighted_trends:
                    weighted_trends[trend] += weight
                trends.append(trend)
        
        # Determine primary trend
        primary_trend = max(weighted_trends, key=weighted_trends.get)
        alignment_score = weighted_trends[primary_trend] / sum(weights.values()) * 100
        
        # Check for trend conflicts
        unique_trends = set(trends)
        is_aligned = len(unique_trends) == 1
        has_conflict = 'up' in unique_trends and 'down' in unique_trends
        
        return {
            'primary_trend': primary_trend,
            'alignment_score': alignment_score,
            'is_aligned': is_aligned,
            'has_conflict': has_conflict,
            'trends_by_timeframe': {tf: metrics[tf].get('trend', 'unknown') 
                                   for tf in metrics}
        }
    
    def _analyze_volatility(self, metrics: Dict) -> Dict:
        """Analyze volatility across timeframes"""
        volatilities = []
        ranges = []
        
        for tf in ['5m', '15m', '30m', '1h']:
            if tf in metrics:
                vol = metrics[tf].get('volatility', 'medium')
                volatilities.append(vol)
                
                range_size = metrics[tf].get('range_size', 0)
                if range_size > 0:
                    ranges.append(range_size)
        
        # Calculate average range as percentage of price
        if ranges and '15m' in metrics:
            current_price = metrics['15m'].get('current_price', 0)
            if current_price > 0:
                avg_range = statistics.mean(ranges)
                range_percent = (avg_range / current_price) * 100
            else:
                range_percent = 0
        else:
            range_percent = 0
        
        # Determine volatility regime
        high_vol_count = volatilities.count('high')
        if high_vol_count >= 2:
            volatility_regime = 'high'
        elif volatilities.count('low') >= 2:
            volatility_regime = 'low'
        else:
            volatility_regime = 'medium'
            
        return {
            'volatility_regime': volatility_regime,
            'range_percent': range_percent,
            'is_expanding': range_percent > 0.5,  # More than 0.5% range
            'is_contracting': range_percent < 0.2  # Less than 0.2% range
        }
    
    def _analyze_momentum(self, metrics: Dict) -> Dict:
        """Analyze momentum across timeframes"""
        momentum_scores = {'strong': 3, 'weak': 1, 'neutral': 2}
        total_score = 0
        count = 0
        
        # Check indicator states
        bullish_indicators = 0
        bearish_indicators = 0
        
        for tf in ['5m', '15m', '30m']:
            if tf in metrics:
                momentum = metrics[tf].get('momentum', 'neutral')
                total_score += momentum_scores.get(momentum, 2)
                count += 1
                
                # Check indicators
                indicators = metrics[tf].get('indicators', {})
                if indicators.get('ATR_crayon') == 'bullish':
                    bullish_indicators += 1
                elif indicators.get('ATR_crayon') == 'bearish':
                    bearish_indicators += 1
                    
                if indicators.get('FSVZO') == 'above_zero':
                    bullish_indicators += 1
                elif indicators.get('FSVZO') == 'below_zero':
                    bearish_indicators += 1
        
        avg_momentum = total_score / count if count > 0 else 2
        
        return {
            'average_momentum_score': avg_momentum,
            'momentum_state': 'strong' if avg_momentum > 2.5 else 'weak' if avg_momentum < 1.5 else 'neutral',
            'bullish_indicators': bullish_indicators,
            'bearish_indicators': bearish_indicators,
            'indicator_bias': 'bullish' if bullish_indicators > bearish_indicators else 
                             'bearish' if bearish_indicators > bullish_indicators else 'neutral'
        }
    
    def _determine_regime(self, trend_analysis: Dict, volatility_analysis: Dict, 
                         momentum_analysis: Dict, metrics: Dict) -> Dict:
        """Determine the primary market regime"""
        regime = self.REGIME_CHOPPY
        confidence = 50
        supporting_factors = []
        
        # Trending regime detection
        if trend_analysis['alignment_score'] > 70 and not trend_analysis['has_conflict']:
            if trend_analysis['primary_trend'] == 'up' and momentum_analysis['momentum_state'] != 'weak':
                regime = self.REGIME_TRENDING_UP
                confidence = min(trend_analysis['alignment_score'], 90)
                supporting_factors.append(f"Strong uptrend alignment ({trend_analysis['alignment_score']:.0f}%)")
            elif trend_analysis['primary_trend'] == 'down' and momentum_analysis['momentum_state'] != 'weak':
                regime = self.REGIME_TRENDING_DOWN
                confidence = min(trend_analysis['alignment_score'], 90)
                supporting_factors.append(f"Strong downtrend alignment ({trend_analysis['alignment_score']:.0f}%)")
        
        # Ranging regime detection
        elif (trend_analysis['primary_trend'] == 'sideways' and 
              volatility_analysis['volatility_regime'] != 'high' and
              not volatility_analysis['is_expanding']):
            regime = self.REGIME_RANGING
            confidence = 70
            supporting_factors.append("Sideways trend with contained volatility")
            
        # Choppy regime detection
        elif (trend_analysis['has_conflict'] or 
              volatility_analysis['volatility_regime'] == 'high' or
              trend_analysis['alignment_score'] < 50):
            regime = self.REGIME_CHOPPY
            confidence = 80
            supporting_factors.append("Conflicting trends or high volatility")
            
        # Breakout detection
        if self._detect_breakout(metrics):
            regime = self.REGIME_BREAKOUT
            confidence = 75
            supporting_factors.append("Potential breakout detected")
            
        # Add momentum factors
        if momentum_analysis['momentum_state'] == 'strong':
            supporting_factors.append("Strong momentum")
        elif momentum_analysis['momentum_state'] == 'weak':
            supporting_factors.append("Weak momentum")
            confidence -= 10
            
        # Add indicator bias
        if momentum_analysis['indicator_bias'] != 'neutral':
            supporting_factors.append(f"{momentum_analysis['indicator_bias'].capitalize()} indicator bias")
            
        return {
            'primary_regime': regime,
            'confidence': max(0, min(100, confidence)),
            'supporting_factors': supporting_factors,
            'trend_details': trend_analysis,
            'volatility_details': volatility_analysis,
            'momentum_details': momentum_analysis
        }
    
    def _detect_breakout(self, metrics: Dict) -> bool:
        """Detect potential breakout conditions"""
        # Check if price is near key levels in multiple timeframes
        breakout_signals = 0
        
        for tf in ['5m', '15m', '30m']:
            if tf not in metrics:
                continue
                
            data = metrics[tf]
            current_price = data.get('current_price', 0)
            resistances = data.get('resistance', [])
            supports = data.get('support', [])
            
            if not current_price or (not resistances and not supports):
                continue
                
            # Check proximity to resistance (potential bullish breakout)
            for resistance in resistances:
                if resistance > current_price:
                    distance_pct = ((resistance - current_price) / current_price) * 100
                    if distance_pct < 0.1:  # Within 0.1% of resistance
                        breakout_signals += 1
                        break
                        
            # Check proximity to support (potential bearish breakout)
            for support in supports:
                if support < current_price:
                    distance_pct = ((current_price - support) / current_price) * 100
                    if distance_pct < 0.1:  # Within 0.1% of support
                        breakout_signals += 1
                        break
                        
        return breakout_signals >= 2
    
    def _get_trade_recommendation(self, regime_result: Dict) -> bool:
        """Determine if trading is recommended in this regime"""
        regime = regime_result['primary_regime']
        confidence = regime_result['confidence']
        
        # Good regimes for trading
        if regime in [self.REGIME_TRENDING_UP, self.REGIME_TRENDING_DOWN]:
            return confidence > 60
        
        # Potentially good for range trading
        elif regime == self.REGIME_RANGING:
            return confidence > 70
            
        # Generally avoid
        elif regime == self.REGIME_CHOPPY:
            return False
            
        # Breakout - only with high confidence
        elif regime == self.REGIME_BREAKOUT:
            return confidence > 75
            
        return False
    
    def _assess_risk_level(self, regime_result: Dict, metrics: Dict) -> str:
        """Assess risk level based on regime and market conditions"""
        regime = regime_result['primary_regime']
        volatility = regime_result['volatility_details']['volatility_regime']
        
        # High risk conditions
        if regime == self.REGIME_CHOPPY or volatility == 'high':
            return 'high'
            
        # Low risk conditions
        elif regime in [self.REGIME_TRENDING_UP, self.REGIME_TRENDING_DOWN] and volatility == 'low':
            return 'low'
            
        # Default to medium
        return 'medium'
    
    def get_regime_trading_rules(self, regime: str) -> Dict:
        """Get specific trading rules for each regime"""
        rules = {
            self.REGIME_TRENDING_UP: {
                'preferred_signal': 'BUY',
                'avoid_signal': 'SELL',
                'stop_loss_multiplier': 1.0,
                'take_profit_multiplier': 1.5,
                'max_position_size': 3,
                'entry_confirmation_required': False
            },
            self.REGIME_TRENDING_DOWN: {
                'preferred_signal': 'SELL',
                'avoid_signal': 'BUY',
                'stop_loss_multiplier': 1.0,
                'take_profit_multiplier': 1.5,
                'max_position_size': 3,
                'entry_confirmation_required': False
            },
            self.REGIME_RANGING: {
                'preferred_signal': 'BOTH',
                'avoid_signal': None,
                'stop_loss_multiplier': 0.75,
                'take_profit_multiplier': 1.0,
                'max_position_size': 2,
                'entry_confirmation_required': True
            },
            self.REGIME_CHOPPY: {
                'preferred_signal': None,
                'avoid_signal': 'BOTH',
                'stop_loss_multiplier': 0.5,
                'take_profit_multiplier': 0.5,
                'max_position_size': 0,
                'entry_confirmation_required': True
            },
            self.REGIME_BREAKOUT: {
                'preferred_signal': 'MOMENTUM',
                'avoid_signal': 'COUNTER',
                'stop_loss_multiplier': 1.25,
                'take_profit_multiplier': 2.0,
                'max_position_size': 2,
                'entry_confirmation_required': True
            }
        }
        
        return rules.get(regime, rules[self.REGIME_CHOPPY])
