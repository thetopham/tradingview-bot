# market_regime.py
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import statistics
import hashlib  
import json     
import time
import pytz

class MarketRegime:
    """
    Market regime detection optimized for day trading with a single 5m timeframe
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
        
        # Configurable timeframe for day trading
        self.timeframes = ['5m']
        self.primary_timeframes = ['5m']

        # Day trading optimized weights (single timeframe)
        self.default_weights = {
            '5m': 1.0
        }

        # Dynamic weight adjustments by market state
        self.market_state_weights = {
            'volatile_open': {
                '5m': 1.0
            },
            'normal': {
                '5m': 1.0
            },
            'lunch_chop': {
                '5m': 1.0
            },
            'power_hour': {
                '5m': 1.0
            }
        }
        
        # Chicago timezone
        self.CT = pytz.timezone("America/Chicago")
        
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
    
    def _get_current_weights(self) -> Dict:
        """Get weights based on current market session"""
        now = datetime.now(self.CT)
        hour = now.hour
        minute = now.minute
        
        # First 30 min of market (8:30-9:00 AM CT)
        if hour == 8 and minute >= 30:
            return self.market_state_weights['volatile_open']
        
        # Lunch period (11:00 AM - 1:00 PM CT)
        elif 11 <= hour < 13:
            return self.market_state_weights['lunch_chop']
        
        # Power hour (2:00 PM - 3:00 PM CT)
        elif 14 <= hour < 15:
            return self.market_state_weights['power_hour']
        
        # Normal trading hours
        else:
            return self.market_state_weights['normal']
    
    def _adjust_confidence_for_session(self, base_confidence: int) -> int:
        """Adjust confidence based on trading session"""
        now = datetime.now(self.CT)
        hour = now.hour
        minute = now.minute
        
        # Lower confidence during lunch
        if 11 <= hour < 13:
            return max(base_confidence - 10, 0)
        
        # Lower confidence in first 5 minutes of market
        elif hour == 8 and 30 <= minute < 35:
            return max(base_confidence - 15, 0)
        
        # Higher confidence during prime hours
        elif (9 <= hour < 11) or (13 <= hour < 15):
            return min(base_confidence + 5, 100)
        
        return base_confidence
        
    def analyze_regime(self, timeframe_data: Dict[str, Dict]) -> Dict:
        """
        Analyze market regime based on multiple timeframe data
        
        Args:
            timeframe_data: Dict with keys like '5m' containing chart analysis
            
        Returns:
            Dict with regime analysis including:
            - primary_regime: Main market regime
            - confidence: Confidence level (0-100)
            - supporting_factors: List of supporting evidence
            - trade_recommendation: Whether to trade in this regime
            - risk_level: Risk assessment (low/medium/high)
        """
        try:
            if not timeframe_data:
                return self._get_fallback_regime("No timeframe data provided")

            # Restrict analysis to configured timeframes (5m only)
            filtered_data = {
                tf: data for tf, data in timeframe_data.items() if tf in self.timeframes
            }

            if not filtered_data:
                self.logger.warning("No 5m timeframe data supplied; falling back to safe regime")
                return self._get_fallback_regime("Missing required 5m timeframe data")

            # Check cache first
            cache_key = self._generate_cache_key(filtered_data)
            cached_result = self._get_cached_result(cache_key)
            
            if cached_result:
                self.logger.debug(f"Using cached regime analysis (key: {cache_key[:8]}...)")
                return cached_result
            
            # Extract key metrics from each timeframe
            metrics = self._extract_metrics(filtered_data)
            
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
            
            # Add day trading specific analysis
            regime_result['scalping_bias'] = self.get_scalping_bias(metrics)
            regime_result['session_quality'] = self._assess_session_quality()
            
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
            },
            'scalping_bias': {'bias': 'neutral', 'confidence': 0},
            'session_quality': 'unknown'
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
                    'signal_confidence': data.get('signal_confidence', 'low'),
                    'ema21_slope': data.get('ema21_slope', 0)
                }
                
            except Exception as e:
                self.logger.error(f"Error extracting metrics for {tf}: {e}")
                continue
                
        return metrics
    
    def _analyze_trend_alignment(self, metrics: Dict) -> Dict:
        """Analyze trend alignment for the 5m timeframe"""
        trends = []
        weighted_trends = {'up': 0, 'down': 0, 'sideways': 0}

        weights = self._get_current_weights()

        for tf in self.timeframes:
            if tf in metrics:
                trend = metrics[tf].get('trend', 'sideways')
                weight = weights.get(tf, self.default_weights.get(tf, 1.0))

                if trend in weighted_trends:
                    weighted_trends[trend] += weight
                trends.append((tf, trend, weight))

        if not trends:
            return {
                'primary_trend': 'unknown',
                'alignment_score': 0,
                'is_aligned': False,
                'has_conflict': True,
                'trends_by_timeframe': {},
                'higher_tf_agreement': False
            }

        primary_trend = max(weighted_trends, key=weighted_trends.get)
        total_weight = sum(weight for _, _, weight in trends)
        alignment_score = (weighted_trends[primary_trend] / total_weight * 100) if total_weight > 0 else 0

        return {
            'primary_trend': primary_trend,
            'alignment_score': alignment_score,
            'is_aligned': alignment_score >= 60,
            'has_conflict': False,
            'trends_by_timeframe': {tf: trend for tf, trend, _ in trends},
            'higher_tf_agreement': False,
            'trend_strength': self._calculate_trend_strength(metrics, primary_trend),
            'fast_vs_slow': 'aligned'
        }
    
    def _calculate_trend_strength(self, metrics: Dict, primary_trend: str) -> str:
        """Calculate the strength of the primary trend"""
        if primary_trend not in ['up', 'down']:
            return 'none'
        
        strength_score = 0
        
        # Check if signals align with trend
        for tf in self.timeframes:
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
        
        # Focus on configured timeframes
        for tf in self.timeframes:
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
        if ranges:
            price_source = None
            for tf in self.timeframes:
                if tf in metrics:
                    price_source = metrics[tf].get('current_price', 0)
                    break
            if price_source and price_source > 0:
                avg_range = statistics.mean(ranges)
                range_percent = (avg_range / price_source) * 100
            else:
                range_percent = 0
        else:
            range_percent = 0
        
        # Determine volatility regime
        volatility_regime = volatilities[0] if volatilities else 'medium'
            
        return {
            'volatility_regime': volatility_regime,
            'range_percent': range_percent,
            'is_expanding': range_percent > 0.5,  # More than 0.5% range
            'is_contracting': range_percent < 0.2,  # Less than 0.2% range
            'intraday_atr': avg_range if ranges else 0
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
        
        for tf in self.timeframes:
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
            'divergence_present': divergence_count > 0,
            'momentum_quality': self._assess_momentum_quality(avg_momentum, divergence_count)
        }
    
    def _assess_momentum_quality(self, avg_momentum: float, divergence_count: int) -> str:
        """Assess the quality of momentum for day trading"""
        if avg_momentum > 2.5 and divergence_count == 0:
            return 'excellent'
        elif avg_momentum > 2.0 and divergence_count <= 1:
            return 'good'
        elif divergence_count > 1:
            return 'divergent'
        else:
            return 'poor'
    
    def _determine_regime(self, trend_analysis: Dict, volatility_analysis: Dict,
                         momentum_analysis: Dict, metrics: Dict) -> Dict:
        """Determine the primary market regime - OPTIMIZED FOR DAY TRADING"""
        regime = self.REGIME_CHOPPY
        confidence = 50
        supporting_factors = []

        # Simplified slope-first check using EMA21 direction to avoid over-complication
        slope_trend = self._get_ema21_slope_trend(metrics)

        if slope_trend['has_slope_data'] and slope_trend['alignment_score'] >= 60:
            if slope_trend['trend'] == 'up':
                regime = self.REGIME_TRENDING_UP
            elif slope_trend['trend'] == 'down':
                regime = self.REGIME_TRENDING_DOWN
            else:
                regime = self.REGIME_RANGING

            confidence = max(confidence, slope_trend['confidence'])
            supporting_factors.append(
                f"EMA21 slope points {slope_trend['trend']} on 5m"
            )
            supporting_factors.append(
                f"Weighted slope {slope_trend['avg_slope']:.4f}"
            )

        # More aggressive regime detection for day trading on 5m
        if regime == self.REGIME_CHOPPY and trend_analysis['alignment_score'] > 60:
            if trend_analysis['primary_trend'] == 'up' and momentum_analysis['momentum_state'] != 'weak':
                regime = self.REGIME_TRENDING_UP
                confidence = min(trend_analysis['alignment_score'] + 10, 90)
                supporting_factors.append(f"5m uptrend with {trend_analysis['alignment_score']:.0f}% alignment")
                if trend_analysis.get('trend_strength') == 'strong':
                    supporting_factors.append("Strong trend signals")
                    confidence += 5
            elif trend_analysis['primary_trend'] == 'down' and momentum_analysis['momentum_state'] != 'weak':
                regime = self.REGIME_TRENDING_DOWN
                confidence = min(trend_analysis['alignment_score'] + 10, 90)
                supporting_factors.append(f"5m downtrend with {trend_analysis['alignment_score']:.0f}% alignment")
                if trend_analysis.get('trend_strength') == 'strong':
                    supporting_factors.append("Strong trend signals")
                    confidence += 5

        # Ranging regime detection
        elif (regime == self.REGIME_CHOPPY and
              trend_analysis['primary_trend'] == 'sideways' and
              volatility_analysis['volatility_regime'] != 'high' and
              not volatility_analysis['is_expanding']):
            regime = self.REGIME_RANGING
            confidence = 70
            supporting_factors.append("Sideways trend with contained volatility")

        # Choppy conditions
        elif (regime == self.REGIME_CHOPPY and trend_analysis['alignment_score'] < 50):
            regime = self.REGIME_CHOPPY
            confidence = 80
            supporting_factors.append("Uncertain trend conditions on 5m")
            if volatility_analysis['volatility_regime'] == 'high':
                supporting_factors.append("High volatility")
                confidence += 5
        
        # Breakout detection with day trading sensitivity
        if self._detect_breakout(metrics):
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
            if (regime == self.REGIME_TRENDING_UP and momentum_analysis['indicator_bias'] == 'bullish') or \
               (regime == self.REGIME_TRENDING_DOWN and momentum_analysis['indicator_bias'] == 'bearish'):
                confidence = min(confidence + 5, 95)
        
        # Session-based confidence adjustment
        confidence = self._adjust_confidence_for_session(confidence)

        return {
            'primary_regime': regime,
            'confidence': max(0, min(100, confidence)),
            'supporting_factors': supporting_factors,
            'trend_details': {**trend_analysis, 'ema21_slope_summary': slope_trend},
            'volatility_details': volatility_analysis,
            'momentum_details': momentum_analysis
        }

    def _get_ema21_slope_trend(self, metrics: Dict) -> Dict:
        """Simplify regime read with weighted EMA21 slopes."""
        slopes = []
        weighted_sum = 0.0
        total_weight = 0.0

        for tf in self.timeframes:
            if tf not in metrics:
                continue

            slope = metrics[tf].get('ema21_slope')
            if slope is None:
                continue

            try:
                slope_val = float(slope)
            except (TypeError, ValueError):
                continue

            weight = self.default_weights.get(tf, 0.33)
            slopes.append({'timeframe': tf, 'slope': slope_val, 'weight': weight})
            weighted_sum += slope_val * weight
            total_weight += weight

        if not slopes or total_weight == 0:
            return {
                'has_slope_data': False,
                'trend': 'unknown',
                'avg_slope': 0.0,
                'alignment_score': 0,
                'confidence': 0,
                'slopes': []
            }

        avg_slope = weighted_sum / total_weight
        positive = sum(1 for s in slopes if s['slope'] > 0)
        negative = sum(1 for s in slopes if s['slope'] < 0)
        alignment_score = (max(positive, negative) / len(slopes)) * 100

        if abs(avg_slope) < 0.01:
            trend = 'sideways'
        else:
            trend = 'up' if avg_slope > 0 else 'down'

        confidence = min(90, 40 + alignment_score / 2 + min(abs(avg_slope) * 100, 20))

        return {
            'has_slope_data': True,
            'trend': trend,
            'avg_slope': avg_slope,
            'alignment_score': alignment_score,
            'confidence': confidence,
            'slopes': slopes
        }
    
    def _detect_breakout(self, metrics: Dict) -> bool:
        """Detect potential breakout conditions - MORE SENSITIVE FOR DAY TRADING"""
        breakout_signals = 0
        
        # Give more weight to faster timeframe for breakout detection
        timeframe_weights = {'5m': 1.5}
        
        for tf in self.timeframes:
            if tf not in metrics:
                continue
                
            data = metrics[tf]
            current_price = data.get('current_price', 0)
            resistances = data.get('resistance', [])
            supports = data.get('support', [])
            
            if not current_price or (not resistances and not supports):
                continue
            
            # More sensitive thresholds for day trading
            proximity_threshold = 0.10
            
            # Get weight for this timeframe
            weight = timeframe_weights.get(tf, 1.0)
            
            # Check proximity to resistance (potential bullish breakout)
            for resistance in resistances:
                if resistance and resistance > current_price:
                    distance_pct = ((resistance - current_price) / current_price) * 100
                    if distance_pct < proximity_threshold:
                        breakout_signals += weight
                        break
            
            # Check proximity to support (potential bearish breakout)
            for support in supports:
                if support and support < current_price:
                    distance_pct = ((current_price - support) / current_price) * 100
                    if distance_pct < proximity_threshold:
                        breakout_signals += weight
                        break
        
        # Lower threshold for day trading (single timeframe)
        return breakout_signals >= 1.0
    
    def _get_trade_recommendation(self, regime_result: Dict) -> bool:
        """Determine if trading is recommended in this regime"""
        regime = regime_result['primary_regime']
        confidence = regime_result['confidence']
        
        # Good regimes for trading
        if regime in [self.REGIME_TRENDING_UP, self.REGIME_TRENDING_DOWN]:
            return confidence > 55  # Lowered from 60 for more opportunities
        
        # Potentially good for range trading
        elif regime == self.REGIME_RANGING:
            return confidence > 65
        
        # Generally avoid choppy markets
        elif regime == self.REGIME_CHOPPY:
            # But allow if confidence is very low (might be misclassified)
            return confidence < 60
        
        # Breakout - only with good confidence
        elif regime == self.REGIME_BREAKOUT:
            return confidence > 70
        
        return False
    
    def _assess_risk_level(self, regime_result: Dict, metrics: Dict) -> str:
        """Assess risk level based on regime and market conditions"""
        regime = regime_result['primary_regime']
        volatility = regime_result['volatility_details']['volatility_regime']
        confidence = regime_result['confidence']
        
        # Session-based risk adjustment
        session_quality = self._assess_session_quality()
        
        # High risk conditions
        if regime == self.REGIME_CHOPPY and confidence > 70:
            return 'high'
        
        if volatility == 'high' and regime != self.REGIME_BREAKOUT:
            return 'high'
        
        if session_quality == 'poor':
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
    
    def _assess_session_quality(self) -> str:
        """Assess current trading session quality"""
        now = datetime.now(self.CT)
        hour = now.hour
        minute = now.minute
        
        # Excellent times
        if (hour == 9 and minute >= 30) or (hour == 10):
            return 'excellent'
        
        # Good times
        elif (hour == 9 and minute < 30) or (13 <= hour < 14):
            return 'good'
        
        # Poor times
        elif 11 <= hour < 13:  # Lunch
            return 'poor'
        
        # First/last 5 minutes
        elif (hour == 8 and minute >= 25 and minute < 35) or \
             (hour == 14 and minute >= 55):
            return 'poor'
        
        return 'normal'
    
    def get_scalping_bias(self, metrics: Dict) -> Dict:
        """Quick bias for scalping decisions using only 5m"""
        if '5m' not in metrics:
            return {'bias': 'neutral', 'confidence': 0, 'entry_allowed': False}

        five_min = metrics['5m']
        trend = five_min.get('trend', 'sideways')
        signal = five_min.get('signal', 'HOLD')

        if trend in ['up', 'down'] and signal in ['BUY', 'SELL']:
            return {
                'bias': trend,
                'confidence': 80,
                'entry_allowed': True,
                'note': '5m trend and signal aligned'
            }
        if trend in ['up', 'down']:
            return {
                'bias': trend,
                'confidence': 65,
                'entry_allowed': True,
                'note': '5m trend bias'
            }

        return {'bias': 'neutral', 'confidence': 40, 'entry_allowed': False}
    
    def get_regime_trading_rules(self, regime: str) -> Dict:
        """Get specific trading rules for each regime - OPTIMIZED FOR DAY TRADING"""
        rules = {
            self.REGIME_TRENDING_UP: {
                'preferred_signal': 'BUY',
                'avoid_signal': 'SELL',
                'stop_loss_multiplier': 1.0,
                'take_profit_multiplier': 1.5,
                'max_position_size': 3,
                'entry_confirmation_required': False,
                'preferred_entry': 'pullback_to_5m_support'
            },
            self.REGIME_TRENDING_DOWN: {
                'preferred_signal': 'SELL',
                'avoid_signal': 'BUY',
                'stop_loss_multiplier': 1.0,
                'take_profit_multiplier': 1.5,
                'max_position_size': 3,
                'entry_confirmation_required': False,
                'preferred_entry': 'pullback_to_5m_resistance'
            },
            self.REGIME_RANGING: {
                'preferred_signal': 'BOTH',
                'avoid_signal': None,
                'stop_loss_multiplier': 0.75,
                'take_profit_multiplier': 1.0,
                'max_position_size': 2,
                'entry_confirmation_required': True,
                'preferred_entry': 'range_extremes'
            },
            self.REGIME_CHOPPY: {
                'preferred_signal': None,
                'avoid_signal': 'BOTH',
                'stop_loss_multiplier': 0.5,
                'take_profit_multiplier': 0.5,
                'max_position_size': 0,
                'entry_confirmation_required': True,
                'preferred_entry': 'avoid'
            },
            self.REGIME_BREAKOUT: {
                'preferred_signal': 'MOMENTUM',
                'avoid_signal': 'COUNTER',
                'stop_loss_multiplier': 1.25,
                'take_profit_multiplier': 2.0,
                'max_position_size': 2,
                'entry_confirmation_required': True,
                'preferred_entry': 'breakout_retest'
            }
        }
        
        return rules.get(regime, rules[self.REGIME_CHOPPY])
