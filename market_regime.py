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
        # ADD THESE NEW LINES FOR CACHING:
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
            # ADD CACHING CHECK:
            cache_key = self._generate_cache_key(timeframe_data)
            cached_result = self._get_cached_result(cache_key)
            
            if cached_result:
                self.logger.debug(f"Using cached regime analysis (key: {cache_key[:8]}...)")
                return cached_result
            
            # EXISTING ANALYSIS CODE continues here...
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
            
            # CACHE THE RESULT before returning:
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
                'trends_by_timeframe': {}
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
                    'volume_trend': data.get('volume_trend', 'flat')
                }
                
            except Exception as e:
                self.logger.error(f"Error extracting metrics for {tf}: {e}")
                continue
                
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
        
        if not trends:
            return {
                'primary_trend': 'unknown',
                'alignment_score': 0,
                'is_aligned': False,
                'has_conflict': True,
                'trends_by_timeframe': {}
            }
        
        # Determine primary trend
        primary_trend = max(weighted_trends, key=weighted_trends.get)
        total_weight = sum(weights[tf] for tf in weights if tf in metrics)
        alignment_score = (weighted_trends[primary_trend] / total_weight * 100) if total_weight > 0 else 0
        
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
                if resistance and resistance > current_price:
                    distance_pct = ((resistance - current_price) / current_price) * 100
                    if distance_pct < 0.1:  # Within 0.1% of resistance
                        breakout_signals += 1
                        break
                        
            # Check proximity to support (potential bearish breakout)
            for support in supports:
                if support and support < current_price:
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
