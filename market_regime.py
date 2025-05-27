# market_regime.py
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import statistics
import hashlib  
import json     
import time    

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
        # Cache management
        self._calculation_cache = {}
        self._cache_expiry = 240  # 4 minutes
        self._last_cache_cleanup = time.time()
        
    def _generate_cache_key(self, timeframe_data: Dict[str, Dict]) -> str:
        """Generate a cache key based on input data"""
        # Create a simplified version of the data for hashing
        key_data = {}
        for tf, data in timeframe_data.items():
            if isinstance(data, dict):
                key_data[tf] = {
                    'trend': data.get('trend_direction', data.get('trend', 'unknown')),
                    'signal': data.get('signal', 'HOLD'),
                    'current_price': data.get('current_price', 0)
                }
        
        # Create hash of the data
        key_string = json.dumps(key_data, sort_keys=True)
        return hashlib.md5(key_string.encode()).hexdigest()
    
    def _get_cached_result(self, cache_key: str) -> Optional[Dict]:
        """Get cached result if still valid"""
        # Clean up old cache entries periodically
        if time.time() - self._last_cache_cleanup > 300:  # Every 5 minutes
            self._cleanup_cache()
            
        if cache_key in self._calculation_cache:
            entry = self._calculation_cache[cache_key]
            if time.time() - entry['timestamp'] < self._cache_expiry:
                return entry['result']
            else:
                # Remove expired entry
                del self._calculation_cache[cache_key]
        return None
    
    def _cache_result(self, cache_key: str, result: Dict):
        """Cache the analysis result"""
        self._calculation_cache[cache_key] = {
            'result': result,
            'timestamp': time.time()
        }
    
    def _cleanup_cache(self):
        """Remove expired cache entries"""
        current_time = time.time()
        expired_keys = [
            key for key, entry in self._calculation_cache.items()
            if current_time - entry['timestamp'] > self._cache_expiry
        ]
        for key in expired_keys:
            del self._calculation_cache[key]
        self._last_cache_cleanup = current_time
        
    def analyze_regime(self, timeframe_data: Dict[str, Dict]) -> Dict:
        """
        Analyze market regime based on multiple timeframe data
        
        Args:
            timeframe_data: Dict with keys like '5m', '15m', '1h' containing chart analysis
            
        Returns:
            Dict with regime analysis including:
            - primary_regime: Main market regime
            - confidence: Confidence level (0-100)
            - supporting_factors: List of supporting evidence
            - trade_recommendation: Whether to trade in this regime
            - risk_level: Risk assessment (low/medium/high)
        """
        try:
            # Check cache first
            cache_key = self._generate_cache_key(timeframe_data)
            cached_result = self._get_cached_result(cache_key)
            
            if cached_result:
                self.logger.debug(f"Using cached regime analysis (key: {cache_key[:8]}...)")
                return cached_result
            
            # Extract key metrics from each timeframe
            metrics = self._extract_metrics(timeframe_data)
            
            # If no valid metrics, return safe defaults
            if not metrics:
                return self._get_fallback_regime("No valid timeframe data")
            
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
            
            # Cache the result
            self._cache_result(cache_key, regime_result)
            
            return regime_result
            
        except Exception as e:
            self.logger.error(f"Error in regime analysis: {e}")
            return self._get_fallback_regime(f"Analysis error: {str(e)}")
    
    def _get_fallback_regime(self, reason: str) -> Dict:
        """Return safe fallback regime when analysis fails"""
        return {
            'primary_regime': self.REGIME_CHOPPY,
            'confidence': 0,
            'supporting_factors': [reason],
            'trade_recommendation': False,
            'risk_level': 'high',
            'trend_details': {
                'primary_trend': 'unknown',
                'alignment_score': 0,
                'is_aligned': False,
                'has_conflict': True,
                'trends_by_timeframe': {},
                'higher_tf_agreement': False
            },
            'volatility_details': {
                'volatility_regime': 'unknown',
                'range_percent': 0,
                'is_expanding': False,
                'is_contracting': False
            },
            'momentum_details': {
                'average_momentum_score': 0,
                'momentum_state': 'neutral',
                'bullish_indicators': 0,
                'bearish_indicators': 0,
                'indicator_bias': 'neutral'
            }
        }
    
    def _extract_metrics(self, timeframe_data: Dict[str, Dict]) -> Dict:
        """Extract key metrics from timeframe data"""
        metrics = {}
        
        for tf, data in timeframe_data.items():
            try:
                if not data:
                    continue
                
                # Handle case where data might be a list or other unexpected type
                if isinstance(data, list):
                    if data:
                        data = data[0]  # Take first item if it's a list
                    else:
                        continue
                
                if not isinstance(data, dict):
                    self.logger.warning(f"Unexpected data type for {tf}: {type(data)}, skipping")
                    continue
                
                metrics[tf] = {
                    'trend': data.get('trend_direction', data.get('trend', 'unknown')),
                    'momentum': data.get('momentum', 'neutral'),
                    'volatility': data.get('volatility', 'medium'),
                    'signal': data.get('signal', 'HOLD'),
                    'support': data.get('support', []),
                    'resistance': data.get('resistance', []),
                    'current_price': data.get('current_price', 0),
                    'range_size': data.get('range_size', 0),
                    'indicators': data.get('indicators', {}),
                    'volume_trend': data.get('volume_trend', 'flat'),
                    'signal_confidence': data.get('signal_confidence', 'low')
                }
                
            except Exception as e:
                self.logger.error(f"Error extracting metrics for {tf}: {e}")
                continue
                
        return metrics
    
    def _analyze_trend_alignment(self, metrics: Dict) -> Dict:
        """Analyze trend alignment across timeframes - IMPROVED VERSION"""
        trends = []
        weighted_trends = {'up': 0, 'down': 0, 'sideways': 0}
        
        # CHANGE: Focus on fewer, more meaningful timeframes
        # Give much more weight to higher timeframes
        weights = {
            '5m': 0.2,   # Short-term
            '15m': 0.3,  # Medium-term  
            '1h': 0.5    # Dominant weight on higher timeframe
        }
        
        # Track trends by timeframe group
        short_term_trends = []  # 5m
        medium_term_trends = [] # 15m
        long_term_trends = []   # 1h
        
        # Also handle legacy 1m and 30m data if present
        legacy_weights = {
            '1m': 0.05,  # Very low weight
            '30m': 0.35  # Between 15m and 1h
        }
        
        # Process main timeframes
        for tf, weight in weights.items():
            if tf in metrics:
                trend = metrics[tf].get('trend', 'sideways')
                if trend in weighted_trends:
                    weighted_trends[trend] += weight
                trends.append(trend)
                
                # Group by timeframe
                if tf == '5m':
                    short_term_trends.append(trend)
                elif tf == '15m':
                    medium_term_trends.append(trend)
                elif tf == '1h':
                    long_term_trends.append(trend)
        
        # Process legacy timeframes if present (with lower impact)
        for tf, weight in legacy_weights.items():
            if tf in metrics:
                trend = metrics[tf].get('trend', 'sideways')
                if trend in weighted_trends:
                    weighted_trends[trend] += weight * 0.5  # Further reduce impact
                
                if tf == '1m':
                    short_term_trends.append(trend)
                elif tf == '30m':
                    medium_term_trends.append(trend)
        
        if not trends:
            return {
                'primary_trend': 'unknown',
                'alignment_score': 0,
                'is_aligned': False,
                'has_conflict': True,
                'trends_by_timeframe': {},
                'higher_tf_agreement': False
            }
        
        # Determine primary trend
        primary_trend = max(weighted_trends, key=weighted_trends.get)
        total_weight = sum(weights.get(tf, 0) for tf in metrics if tf in weights)
        # Add legacy weights if present
        total_weight += sum(legacy_weights.get(tf, 0) * 0.5 for tf in metrics if tf in legacy_weights)
        
        alignment_score = (weighted_trends[primary_trend] / total_weight * 100) if total_weight > 0 else 0
        
        # Smart conflict detection - only care about higher timeframe conflicts
        higher_tf_trends = medium_term_trends + long_term_trends
        unique_higher_tf_trends = set(higher_tf_trends)
        
        # Only flag conflict if HIGHER timeframes disagree significantly
        has_conflict = len(unique_higher_tf_trends) > 1 and 'up' in unique_higher_tf_trends and 'down' in unique_higher_tf_trends
        
        # Consider it aligned if just the important timeframes agree
        is_aligned = len(unique_higher_tf_trends) == 1 or alignment_score > 80
        
        # Check if at least 15m and 1h agree (most important)
        higher_tf_agreement = False
        if '15m' in metrics and '1h' in metrics:
            higher_tf_agreement = metrics['15m']['trend'] == metrics['1h']['trend']
        
        return {
            'primary_trend': primary_trend,
            'alignment_score': alignment_score,
            'is_aligned': is_aligned,
            'has_conflict': has_conflict,
            'trends_by_timeframe': {tf: metrics[tf].get('trend', 'unknown') 
                                   for tf in metrics},
            'higher_tf_agreement': higher_tf_agreement,
            'trend_strength': self._calculate_trend_strength(metrics, primary_trend)
        }
    
    def _calculate_trend_strength(self, metrics: Dict, primary_trend: str) -> str:
        """Calculate the strength of the primary trend"""
        if primary_trend not in ['up', 'down']:
            return 'none'
        
        strength_score = 0
        
        # Check if signals align with trend
        for tf in ['5m', '15m', '30m']:
            if tf in metrics:
                signal = metrics[tf].get('signal', 'HOLD')
                if (primary_trend == 'up' and signal == 'BUY') or \
                   (primary_trend == 'down' and signal == 'SELL'):
                    strength_score += 1
                    
                # Check signal confidence
                if metrics[tf].get('signal_confidence') == 'high':
                    strength_score += 0.5
        
        if strength_score >= 2.5:
            return 'strong'
        elif strength_score >= 1.5:
            return 'moderate'
        else:
            return 'weak'
    
    def _analyze_volatility(self, metrics: Dict) -> Dict:
        """Analyze volatility across timeframes"""
        volatilities = []
        ranges = []
        
        # Focus on key timeframes
        for tf in ['5m', '15m', '30m']:
            if tf in metrics:
                vol = metrics[tf].get('volatility', 'medium')
                volatilities.append(vol)
                
                range_size = metrics[tf].get('range_size', 0)
                if range_size > 0:
                    ranges.append(range_size)
        
        # Include 30m if available
        if '30m' in metrics:
            vol = metrics['30m'].get('volatility', 'medium')
            volatilities.append(vol)
            range_size = metrics['30m'].get('range_size', 0)
            if range_size > 0:
                ranges.append(range_size)
        
        if not volatilities:
            return {
                'volatility_regime': 'unknown',
                'range_percent': 0,
                'is_expanding': False,
                'is_contracting': False
            }
        
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
        momentum_scores = {'accelerating': 3, 'steady': 2, 'decelerating': 1, 'neutral': 2}
        total_score = 0
        count = 0
        
        # Check indicator states
        bullish_indicators = 0
        bearish_indicators = 0
        divergence_count = 0
        
        for tf in ['5m', '15m', '30m']:
            if tf in metrics:
                # Handle different momentum naming conventions
                momentum = metrics[tf].get('momentum', 'neutral')
                if momentum in ['strong', 'accelerating']:
                    score = 3
                elif momentum in ['weak', 'decelerating']:
                    score = 1
                else:
                    score = 2
                    
                total_score += score
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
                    
                # Check for divergences
                if indicators.get('Fisher') == 'bullish_divergence':
                    divergence_count += 1
                    bullish_indicators += 0.5
                elif indicators.get('Fisher') == 'bearish_divergence':
                    divergence_count += 1
                    bearish_indicators += 0.5
        
        avg_momentum = total_score / count if count > 0 else 2
        
        # Determine momentum state
        if avg_momentum > 2.5:
            momentum_state = 'strong'
        elif avg_momentum < 1.5:
            momentum_state = 'weak'
        else:
            momentum_state = 'neutral'
        
        return {
            'average_momentum_score': avg_momentum,
            'momentum_state': momentum_state,
            'bullish_indicators': bullish_indicators,
            'bearish_indicators': bearish_indicators,
            'indicator_bias': 'bullish' if bullish_indicators > bearish_indicators + 1 else 
                             'bearish' if bearish_indicators > bullish_indicators + 1 else 'neutral',
            'divergence_present': divergence_count > 0
        }
    
    def _determine_regime(self, trend_analysis: Dict, volatility_analysis: Dict, 
                         momentum_analysis: Dict, metrics: Dict) -> Dict:
        """Determine the primary market regime - IMPROVED VERSION"""
        regime = self.REGIME_CHOPPY
        confidence = 50
        supporting_factors = []
        
        # CHANGE: More lenient trending regime detection
        # Lower threshold from 70 to 60 for alignment
        if trend_analysis['alignment_score'] > 60:
            # Check if at least higher timeframes agree
            if trend_analysis.get('higher_tf_agreement', False) or not trend_analysis['has_conflict']:
                if trend_analysis['primary_trend'] == 'up' and momentum_analysis['momentum_state'] != 'weak':
                    regime = self.REGIME_TRENDING_UP
                    confidence = min(trend_analysis['alignment_score'] + 10, 90)  # Boost confidence
                    supporting_factors.append(f"Uptrend with {trend_analysis['alignment_score']:.0f}% alignment")
                    if trend_analysis.get('higher_tf_agreement'):
                        supporting_factors.append("Higher timeframes in agreement")
                        confidence += 5
                    if trend_analysis.get('trend_strength') == 'strong':
                        supporting_factors.append("Strong trend signals")
                        confidence += 5
                elif trend_analysis['primary_trend'] == 'down' and momentum_analysis['momentum_state'] != 'weak':
                    regime = self.REGIME_TRENDING_DOWN
                    confidence = min(trend_analysis['alignment_score'] + 10, 90)
                    supporting_factors.append(f"Downtrend with {trend_analysis['alignment_score']:.0f}% alignment")
                    if trend_analysis.get('higher_tf_agreement'):
                        supporting_factors.append("Higher timeframes in agreement")
                        confidence += 5
                    if trend_analysis.get('trend_strength') == 'strong':
                        supporting_factors.append("Strong trend signals")
                        confidence += 5
        
        # Alternative: If alignment is 50-60% but higher timeframes strongly agree
        elif trend_analysis['alignment_score'] > 50 and trend_analysis.get('higher_tf_agreement', False):
            if trend_analysis['primary_trend'] == 'up' and momentum_analysis['indicator_bias'] == 'bullish':
                regime = self.REGIME_TRENDING_UP
                confidence = 65
                supporting_factors.append("Higher timeframes aligned bullish")
            elif trend_analysis['primary_trend'] == 'down' and momentum_analysis['indicator_bias'] == 'bearish':
                regime = self.REGIME_TRENDING_DOWN
                confidence = 65
                supporting_factors.append("Higher timeframes aligned bearish")
        
        # Ranging regime detection
        elif (trend_analysis['primary_trend'] == 'sideways' and 
              volatility_analysis['volatility_regime'] != 'high' and
              not volatility_analysis['is_expanding']):
            regime = self.REGIME_RANGING
            confidence = 70
            supporting_factors.append("Sideways trend with contained volatility")
            
        # Be more specific about choppy conditions
        # Only call it choppy if there's real disagreement in higher timeframes
        elif (trend_analysis['has_conflict'] and trend_analysis['alignment_score'] < 50):
            regime = self.REGIME_CHOPPY
            confidence = 80
            supporting_factors.append("Significant timeframe conflicts")
            if volatility_analysis['volatility_regime'] == 'high':
                supporting_factors.append("High volatility")
                confidence += 5
        
        # If we still haven't determined regime, check the dominant timeframe
        if regime == self.REGIME_CHOPPY and '1h' in metrics:
            # Trust the 1h timeframe as a tiebreaker
            hourly_trend = metrics['1h'].get('trend', 'sideways')
            hourly_signal = metrics['1h'].get('signal', 'HOLD')
            
            if hourly_trend == 'up' and hourly_signal == 'BUY':
                regime = self.REGIME_TRENDING_UP
                confidence = 65
                supporting_factors = ["Hourly timeframe showing uptrend"]
            elif hourly_trend == 'down' and hourly_signal == 'SELL':
                regime = self.REGIME_TRENDING_DOWN
                confidence = 65
                supporting_factors = ["Hourly timeframe showing downtrend"]
        
        # Breakout detection
        if self._detect_breakout(metrics):
            # Don't override a trending regime with breakout
            if regime not in [self.REGIME_TRENDING_UP, self.REGIME_TRENDING_DOWN]:
                regime = self.REGIME_BREAKOUT
                confidence = 75
                supporting_factors.append("Potential breakout detected")
            else:
                supporting_factors.append("Breakout within trend")
                confidence += 5
        
        # Check for potential reversal
        if momentum_analysis.get('divergence_present', False):
            if regime in [self.REGIME_TRENDING_UP, self.REGIME_TRENDING_DOWN]:
                supporting_factors.append("Divergence warning - potential reversal")
                confidence -= 10
        
        # Adjust confidence based on momentum
        if momentum_analysis['momentum_state'] == 'strong':
            supporting_factors.append("Strong momentum")
            confidence = min(confidence + 5, 95)
        elif momentum_analysis['momentum_state'] == 'weak':
            supporting_factors.append("Weak momentum")
            confidence -= 10
        
        # Add indicator bias
        if momentum_analysis['indicator_bias'] != 'neutral':
            supporting_factors.append(f"{momentum_analysis['indicator_bias'].capitalize()} indicator bias")
            # Boost confidence if indicators align with regime
            if (regime == self.REGIME_TRENDING_UP and momentum_analysis['indicator_bias'] == 'bullish') or \
               (regime == self.REGIME_TRENDING_DOWN and momentum_analysis['indicator_bias'] == 'bearish'):
                confidence = min(confidence + 5, 95)
        
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
                if resistance and resistance > current_price:
                    distance_pct = ((resistance - current_price) / current_price) * 100
                    if distance_pct < 0.15:  # Within 0.15% of resistance
                        breakout_signals += 1
                        break
            
            # Check proximity to support (potential bearish breakout)
            for support in supports:
                if support and support < current_price:
                    distance_pct = ((current_price - support) / current_price) * 100
                    if distance_pct < 0.15:  # Within 0.15% of support
                        breakout_signals += 1
                        break
        
        return breakout_signals >= 2
    
    def _get_trade_recommendation(self, regime_result: Dict) -> bool:
        """Determine if trading is recommended in this regime"""
        regime = regime_result['primary_regime']
        confidence = regime_result['confidence']
        
        # Good regimes for trading
        if regime in [self.REGIME_TRENDING_UP, self.REGIME_TRENDING_DOWN]:
            return confidence > 55  # Lowered from 60
        
        # Potentially good for range trading
        elif regime == self.REGIME_RANGING:
            return confidence > 65  # Slightly lowered from 70
        
        # Generally avoid choppy markets
        elif regime == self.REGIME_CHOPPY:
            # But allow if confidence is very low (might be misclassified)
            return confidence < 60
        
        # Breakout - only with good confidence
        elif regime == self.REGIME_BREAKOUT:
            return confidence > 70  # Lowered from 75
        
        return False
    
    def _assess_risk_level(self, regime_result: Dict, metrics: Dict) -> str:
        """Assess risk level based on regime and market conditions"""
        regime = regime_result['primary_regime']
        volatility = regime_result['volatility_details']['volatility_regime']
        confidence = regime_result['confidence']
        
        # High risk conditions
        if regime == self.REGIME_CHOPPY and confidence > 70:
            return 'high'
        
        if volatility == 'high' and regime != self.REGIME_BREAKOUT:
            return 'high'
        
        # Low risk conditions
        if regime in [self.REGIME_TRENDING_UP, self.REGIME_TRENDING_DOWN]:
            if confidence > 75 and volatility == 'low':
                return 'low'
            elif confidence > 65:
                return 'medium'
        
        # Ranging with good confidence
        if regime == self.REGIME_RANGING and confidence > 70:
            return 'medium'
        
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
