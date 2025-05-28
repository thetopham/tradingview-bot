# market_regime_ohlc.py - Complete working version

import numpy as np
import logging
from typing import Dict, List, Optional, Tuple, Any
import statistics

class OHLCRegimeDetector:
    """OHLC-based regime detection using price and indicator data"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    def analyze_regime(self, ohlc_data: Dict[str, Dict]) -> Dict:
        """
        Analyze market regime using OHLC data from multiple timeframes
        
        Args:
            ohlc_data: Dict with timeframe keys ('5m', '15m', '30m') containing OHLC arrays
            
        Returns:
            Dict with regime analysis
        """
        try:
            self.logger.info(f"Starting OHLC regime analysis with timeframes: {list(ohlc_data.keys())}")
            
            timeframe_analysis = {}
            
            # Analyze each timeframe
            for tf, data in ohlc_data.items():
                if data and isinstance(data, dict) and 'close' in data:
                    try:
                        analysis = self._analyze_single_timeframe(data, tf)
                        timeframe_analysis[tf] = analysis
                        self.logger.info(f"✅ {tf}: {analysis.get('signal', 'HOLD')} signal, {analysis.get('trend', 'unknown')} trend")
                    except Exception as e:
                        self.logger.error(f"Error analyzing {tf}: {e}")
                        continue
                else:
                    self.logger.warning(f"Invalid data for {tf}: {type(data)}")
            
            if not timeframe_analysis:
                self.logger.warning("No timeframes successfully analyzed")
                return self._get_default_regime()
            
            # Combine timeframe analysis into overall regime
            regime_result = self._determine_overall_regime(timeframe_analysis)
            regime_result['timeframe_analysis'] = timeframe_analysis
            
            self.logger.info(f"Overall regime: {regime_result['primary_regime']} (confidence: {regime_result['confidence']}%)")
            
            return regime_result
            
        except Exception as e:
            self.logger.error(f"Error in OHLC regime analysis: {e}")
            return self._get_default_regime()
    
    def _analyze_single_timeframe(self, data: Dict[str, List], timeframe: str) -> Dict:
        """Analyze a single timeframe's OHLC data - ENHANCED WITH INDICATORS"""
        
        try:
            # Extract basic OHLC arrays
            closes = np.array(data['close'])
            highs = np.array(data['high'])
            lows = np.array(data['low'])
            opens = np.array(data['open'])
            volumes = np.array(data.get('volume', [0] * len(closes)))
            
            # Extract indicators (with defaults if not present)
            rsi_values = np.array(data.get('rsi', [50] * len(closes)))
            macd_hist = np.array(data.get('macd_hist', [0] * len(closes)))
            atr_values = np.array(data.get('atr', [10] * len(closes)))
            fisher_values = np.array(data.get('fisher', [0] * len(closes)))
            vzo_values = np.array(data.get('vzo', [0] * len(closes)))
            phobos_values = np.array(data.get('phobos', [0] * len(closes)))
            stoch_k = np.array(data.get('stoch_k', [50] * len(closes)))
            bb_upper = np.array(data.get('bb_upper', highs))
            bb_middle = np.array(data.get('bb_middle', closes))
            bb_lower = np.array(data.get('bb_lower', lows))
            
            if len(closes) < 5:
                return {'signal': 'HOLD', 'trend': 'unknown', 'confidence': 0}
            
            # Enhanced trend detection
            trend = self._calculate_trend(closes, highs, lows)
            
            # Enhanced volatility with ATR and Bollinger Bands
            volatility = self._calculate_enhanced_volatility(closes, highs, lows, atr_values, bb_upper, bb_lower)
            
            # Enhanced momentum with multiple indicators
            momentum = self._calculate_enhanced_momentum(closes, rsi_values, macd_hist, fisher_values, vzo_values, phobos_values)
            
            # Support/Resistance with Bollinger Bands
            support_resistance = self._find_support_resistance(highs, lows, closes, bb_upper, bb_lower, bb_middle)
            
            # Generate enhanced signal
            signal_info = self._generate_enhanced_signal(
                trend, momentum, volatility, closes[-1], support_resistance,
                {'rsi': rsi_values[-1] if len(rsi_values) > 0 else 50,
                 'macd': macd_hist[-1] if len(macd_hist) > 0 else 0,
                 'fisher': fisher_values[-1] if len(fisher_values) > 0 else 0,
                 'vzo': vzo_values[-1] if len(vzo_values) > 0 else 0}
            )
            
            return {
                'trend': trend['direction'],
                'trend_strength': trend['strength'],
                'momentum': momentum['state'],
                'momentum_score': momentum.get('indicator_score', 50),
                'volatility': volatility['level'],
                'support': support_resistance['support'],
                'resistance': support_resistance['resistance'],
                'signal': signal_info['signal'],
                'signal_confidence': signal_info['confidence'],
                'current_price': float(closes[-1]),
                'recent_high': float(highs[-5:].max()),
                'recent_low': float(lows[-5:].min()),
                'timeframe': timeframe,
                'indicators': {
                    'rsi': float(rsi_values[-1]) if len(rsi_values) > 0 else 50,
                    'macd_hist': float(macd_hist[-1]) if len(macd_hist) > 0 else 0,
                    'fisher': float(fisher_values[-1]) if len(fisher_values) > 0 else 0,
                    'vzo': float(vzo_values[-1]) if len(vzo_values) > 0 else 0
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error analyzing {timeframe} timeframe: {e}")
            return {'signal': 'HOLD', 'trend': 'unknown', 'confidence': 0}
    
    def _calculate_trend(self, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> Dict:
        """Calculate trend direction and strength"""
        if len(closes) < 5:
            return {'direction': 'unknown', 'strength': 'weak'}
        
        try:
            # Simple linear regression slope
            x = np.arange(len(closes))
            slope = np.polyfit(x, closes, 1)[0]
            
            # Convert slope to percentage
            slope_pct = (slope / closes[0]) * 100 if closes[0] != 0 else 0
            
            # Check for higher highs / lower lows
            recent_highs = highs[-3:]
            recent_lows = lows[-3:]
            
            higher_highs = len(recent_highs) > 1 and recent_highs[-1] > recent_highs[0]
            lower_lows = len(recent_lows) > 1 and recent_lows[-1] < recent_lows[0]
            
            if slope_pct > 0.1:
                direction = 'up'
                strength = 'strong' if slope_pct > 0.3 and higher_highs else 'moderate'
            elif slope_pct < -0.1:
                direction = 'down' 
                strength = 'strong' if slope_pct < -0.3 and lower_lows else 'moderate'
            else:
                direction = 'sideways'
                strength = 'weak'
            
            return {
                'direction': direction,
                'strength': strength,
                'slope': slope_pct,
                'higher_highs': higher_highs,
                'lower_lows': lower_lows
            }
        except Exception as e:
            self.logger.error(f"Error calculating trend: {e}")
            return {'direction': 'sideways', 'strength': 'weak'}
    
    def _calculate_enhanced_volatility(self, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, 
                                     atr_values: np.ndarray, bb_upper: np.ndarray, bb_lower: np.ndarray) -> Dict:
        """Enhanced volatility calculation with ATR and Bollinger Bands"""
        try:
            if len(closes) < 5:
                return {'level': 'medium', 'atr': 0}
            
            # ATR-based volatility
            if len(atr_values) > 0 and atr_values[-1] > 0:
                current_atr = atr_values[-1]
                avg_atr = np.mean(atr_values[-5:]) if len(atr_values) >= 5 else current_atr
            else:
                # Fallback: simple range calculation
                ranges = highs - lows
                current_atr = np.mean(ranges[-3:])
                avg_atr = current_atr
            
            # Bollinger Band width
            if len(bb_upper) > 0 and len(bb_lower) > 0:
                bb_width = bb_upper[-1] - bb_lower[-1]
                bb_width_avg = np.mean(bb_upper[-5:] - bb_lower[-5:]) if len(bb_upper) >= 5 else bb_width
                bb_squeeze = bb_width < bb_width_avg * 0.8
            else:
                bb_width = 0
                bb_squeeze = False
            
            # Determine volatility level
            avg_price = np.mean(closes[-5:])
            atr_pct = (current_atr / avg_price) * 100 if avg_price != 0 else 0
            
            if atr_pct > 0.5:
                level = 'high'
            elif atr_pct < 0.2:
                level = 'low'
            else:
                level = 'medium'
            
            return {
                'level': level,
                'atr': float(current_atr),
                'atr_percent': atr_pct,
                'bb_squeeze': bb_squeeze,
                'expanding': current_atr > avg_atr * 1.1,
                'contracting': current_atr < avg_atr * 0.9
            }
        except Exception as e:
            self.logger.error(f"Error calculating volatility: {e}")
            return {'level': 'medium', 'atr': 0}
    
    def _calculate_enhanced_momentum(self, closes: np.ndarray, rsi: np.ndarray, macd_hist: np.ndarray, 
                                   fisher: np.ndarray, vzo: np.ndarray, phobos: np.ndarray) -> Dict:
        """Enhanced momentum using multiple indicators"""
        try:
            if len(closes) < 3:
                return {'state': 'neutral', 'indicator_score': 50}
            
            # Price momentum
            price_change = closes[-1] - closes[-3] if len(closes) >= 3 else 0
            price_change_pct = (price_change / closes[-3]) * 100 if len(closes) >= 3 and closes[-3] != 0 else 0
            
            # Indicator momentum score (0-100 scale)
            momentum_score = 50  # Start neutral
            
            # RSI momentum (weight: 25%)
            if len(rsi) > 0:
                current_rsi = rsi[-1]
                if current_rsi > 70:
                    momentum_score += 12.5
                elif current_rsi > 60:
                    momentum_score += 6.25
                elif current_rsi < 30:
                    momentum_score -= 12.5
                elif current_rsi < 40:
                    momentum_score -= 6.25
            
            # MACD momentum (weight: 25%)
            if len(macd_hist) >= 2:
                current_macd = macd_hist[-1]
                prev_macd = macd_hist[-2]
                
                if current_macd > 0:
                    if current_macd > prev_macd:
                        momentum_score += 12.5
                    else:
                        momentum_score += 6.25
                elif current_macd < 0:
                    if current_macd < prev_macd:
                        momentum_score -= 12.5
                    else:
                        momentum_score -= 6.25
            
            # VZO momentum (weight: 20%)
            if len(vzo) > 0:
                current_vzo = vzo[-1]
                if current_vzo > 40:
                    momentum_score += 10
                elif current_vzo > 15:
                    momentum_score += 5
                elif current_vzo < -40:
                    momentum_score -= 10
                elif current_vzo < -15:
                    momentum_score -= 5
            
            # Fisher momentum (weight: 15%)
            if len(fisher) > 0:
                current_fisher = fisher[-1]
                if current_fisher > 1.5:
                    momentum_score += 7.5
                elif current_fisher > 0.5:
                    momentum_score += 3.75
                elif current_fisher < -1.5:
                    momentum_score -= 7.5
                elif current_fisher < -0.5:
                    momentum_score -= 3.75
            
            # Phobos momentum (weight: 15%)
            if len(phobos) > 0:
                current_phobos = phobos[-1]
                if current_phobos > 0.5:
                    momentum_score += 7.5
                elif current_phobos < -0.5:
                    momentum_score -= 7.5
            
            # Determine state
            if momentum_score >= 70 and price_change_pct > 0.1:
                state = 'accelerating'
            elif momentum_score <= 30 and price_change_pct < -0.1:
                state = 'decelerating'
            elif 40 <= momentum_score <= 60:
                state = 'steady'
            elif momentum_score > 60:
                state = 'steady_bullish'
            else:
                state = 'steady_bearish'
            
            return {
                'state': state,
                'indicator_score': float(momentum_score),
                'price_change_pct': price_change_pct
            }
        except Exception as e:
            self.logger.error(f"Error calculating momentum: {e}")
            return {'state': 'neutral', 'indicator_score': 50}
    
    def _find_support_resistance(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                               bb_upper: np.ndarray, bb_lower: np.ndarray, bb_middle: np.ndarray) -> Dict:
        """Enhanced support/resistance with Bollinger Bands"""
        try:
            if len(highs) < 5:
                return {'support': [], 'resistance': []}
            
            # Basic levels
            recent_high = float(highs[-5:].max())
            recent_low = float(lows[-5:].min())
            
            support_levels = [recent_low]
            resistance_levels = [recent_high]
            
            # Add Bollinger Band levels if available
            if len(bb_upper) > 0 and len(bb_lower) > 0:
                resistance_levels.append(float(bb_upper[-1]))
                support_levels.append(float(bb_lower[-1]))
                
                # Add middle band
                if len(bb_middle) > 0:
                    middle = float(bb_middle[-1])
                    current_price = float(closes[-1])
                    
                    if current_price > middle:
                        support_levels.append(middle)
                    else:
                        resistance_levels.append(middle)
            
            # Remove duplicates and sort
            support_levels = sorted(list(set(support_levels)), reverse=True)[:3]
            resistance_levels = sorted(list(set(resistance_levels)))[:3]
            
            return {
                'support': support_levels,
                'resistance': resistance_levels,
                'nearest_support': support_levels[0] if support_levels else recent_low,
                'nearest_resistance': resistance_levels[0] if resistance_levels else recent_high
            }
        except Exception as e:
            self.logger.error(f"Error finding S/R levels: {e}")
            return {'support': [], 'resistance': []}
    
    def _generate_enhanced_signal(self, trend: Dict, momentum: Dict, volatility: Dict, 
                                current_price: float, sr: Dict, indicators: Dict) -> Dict:
        """Generate enhanced trading signal"""
        try:
            signal = "HOLD"
            confidence = 50
            
            # Trend-following with momentum confirmation
            if trend['direction'] == 'up' and trend['strength'] in ['strong', 'moderate']:
                if momentum['indicator_score'] > 60:
                    signal = "BUY"
                    confidence = 75 if trend['strength'] == 'strong' else 65
                    
                    # Bonus for momentum alignment
                    if momentum['state'] == 'accelerating':
                        confidence += 5
            
            elif trend['direction'] == 'down' and trend['strength'] in ['strong', 'moderate']:
                if momentum['indicator_score'] < 40:
                    signal = "SELL"
                    confidence = 75 if trend['strength'] == 'strong' else 65
                    
                    # Bonus for momentum alignment
                    if momentum['state'] == 'decelerating':
                        confidence += 5
            
            # Range trading (if no clear trend)
            elif trend['direction'] == 'sideways':
                # Near support - potential buy
                if sr.get('nearest_support') and current_price <= sr['nearest_support'] * 1.001:
                    if indicators['rsi'] < 40:
                        signal = "BUY"
                        confidence = 60
                
                # Near resistance - potential sell
                elif sr.get('nearest_resistance') and current_price >= sr['nearest_resistance'] * 0.999:
                    if indicators['rsi'] > 60:
                        signal = "SELL"
                        confidence = 60
            
            # Adjust for volatility
            if volatility['level'] == 'high':
                confidence -= 10
            elif volatility['level'] == 'low' and signal != 'HOLD':
                confidence += 5
            
            # Bollinger squeeze bonus
            if volatility.get('bb_squeeze') and signal != 'HOLD':
                confidence += 5
            
            return {
                'signal': signal,
                'confidence': max(0, min(100, confidence))
            }
        except Exception as e:
            self.logger.error(f"Error generating signal: {e}")
            return {'signal': 'HOLD', 'confidence': 50}
    
    def _determine_overall_regime(self, timeframe_analysis: Dict) -> Dict:
        """Determine overall market regime from timeframe analysis"""
    
        if not timeframe_analysis:
            return self._get_default_regime()
    
        try:
            # Extract signals and trends
            signals = []
            trends = []
            confidences = []
            momentum_scores = []
            volatility_levels = []
        
            for tf, tf_data in timeframe_analysis.items():
                signals.append(tf_data.get('signal', 'HOLD'))
                trends.append(tf_data.get('trend', 'unknown'))
                confidences.append(tf_data.get('signal_confidence', 'low'))
                momentum_scores.append(tf_data.get('momentum_score', 50))
                volatility_levels.append(tf_data.get('volatility', 'medium'))
        
            # Count occurrences
            buy_count = signals.count('BUY')
            sell_count = signals.count('SELL')
            up_count = trends.count('up')
            down_count = trends.count('down')
            sideways_count = trends.count('sideways')
        
            # Determine primary regime
            total_tfs = len(signals)
        
            if buy_count >= total_tfs * 0.6 or up_count >= total_tfs * 0.6:
                primary_regime = 'trending_up'
                confidence = min(85, 60 + (buy_count + up_count) * 8)
                factors = [f"{buy_count}/{total_tfs} BUY signals", f"{up_count}/{total_tfs} uptrends"]
                primary_trend = 'up'
            
            elif sell_count >= total_tfs * 0.6 or down_count >= total_tfs * 0.6:
                primary_regime = 'trending_down'
                confidence = min(85, 60 + (sell_count + down_count) * 8)
                factors = [f"{sell_count}/{total_tfs} SELL signals", f"{down_count}/{total_tfs} downtrends"]
                primary_trend = 'down'
            
            elif sideways_count >= total_tfs * 0.6:
                primary_regime = 'ranging'
                confidence = 70
                factors = [f"{sideways_count}/{total_tfs} sideways trends", "Range-bound market"]
                primary_trend = 'sideways'
            
            else:
                primary_regime = 'choppy'
                confidence = 55
                factors = ["Mixed signals across timeframes", "No clear directional bias"]
                primary_trend = 'sideways'
        
            # Calculate alignment score
            if primary_trend in ['up', 'down']:
                aligned_count = up_count if primary_trend == 'up' else down_count
                alignment_score = (aligned_count / total_tfs * 100) if total_tfs > 0 else 0
            else:
                alignment_score = (sideways_count / total_tfs * 100) if total_tfs > 0 else 0
        
            # Determine volatility regime
            high_vol = volatility_levels.count('high')
            low_vol = volatility_levels.count('low')
            if high_vol > total_tfs / 2:
                volatility_regime = 'high'
            elif low_vol > total_tfs / 2:
                volatility_regime = 'low'
            else:
                volatility_regime = 'medium'
        
            # Calculate momentum state
            avg_momentum = sum(momentum_scores) / len(momentum_scores) if momentum_scores else 50
            if avg_momentum > 65:
                momentum_state = 'strong'
            elif avg_momentum < 35:
                momentum_state = 'weak'
            else:
                momentum_state = 'neutral'
        
            # Check for higher timeframe agreement (15m and 30m)
            higher_tf_trends = []
            for tf in ['15m', '30m']:
                if tf in timeframe_analysis:
                    higher_tf_trends.append(timeframe_analysis[tf].get('trend', 'unknown'))
        
            higher_tf_agreement = len(set(higher_tf_trends)) == 1 and 'unknown' not in higher_tf_trends
        
            # Build trend details dict
            trends_by_timeframe = {}
            for tf, tf_data in timeframe_analysis.items():
                trends_by_timeframe[tf] = tf_data.get('trend', 'unknown')
        
            # Adjust confidence based on signal quality
            high_conf_signals = sum(1 for c in confidences if c == 'high')
            if high_conf_signals >= total_tfs * 0.5:
                confidence += 5
        
            confidence = max(30, min(95, confidence))
        
            # Risk level based on confidence and volatility
            if confidence > 80 and volatility_regime != 'high':
                risk_level = 'low'
            elif confidence > 65 or volatility_regime == 'low':
                risk_level = 'medium'
            else:
                risk_level = 'high'
        
            return {
                'primary_regime': primary_regime,
                'confidence': confidence,
                'supporting_factors': factors,
                'trade_recommendation': confidence > 65 and primary_regime != 'choppy',
                'risk_level': risk_level,
            
                # CRITICAL: Add these nested dictionaries that the system expects
                'trend_details': {
                    'primary_trend': primary_trend,
                    'alignment_score': alignment_score,
                    'is_aligned': alignment_score > 70,
                    'has_conflict': (up_count > 0 and down_count > 0) or buy_count > 0 and sell_count > 0,
                    'trends_by_timeframe': trends_by_timeframe,
                    'higher_tf_agreement': higher_tf_agreement,
                    'trend_strength': 'strong' if alignment_score > 80 else 'moderate' if alignment_score > 60 else 'weak',
                    'fast_vs_slow': 'aligned' if alignment_score > 70 else 'divergent'
                },
            
                'volatility_details': {
                    'volatility_regime': volatility_regime,
                    'range_percent': 0.3 if volatility_regime == 'medium' else 0.5 if volatility_regime == 'high' else 0.2,
                    'is_expanding': volatility_regime == 'high',
                    'is_contracting': volatility_regime == 'low',
                    'intraday_atr': 10  # Could calculate from actual ATR data
                },
            
                'momentum_details': {
                    'average_momentum_score': avg_momentum,
                    'momentum_state': momentum_state,
                    'bullish_indicators': buy_count,
                    'bearish_indicators': sell_count,
                    'indicator_bias': 'bullish' if buy_count > sell_count + 1 else 'bearish' if sell_count > buy_count + 1 else 'neutral',
                    'divergence_present': False,  # Could be enhanced
                    'momentum_quality': 'good' if momentum_state == 'strong' and alignment_score > 70 else 'poor'
                },
            
                # Include session quality and scalping bias (expected by MarketRegime)
                'scalping_bias': {
                    'bias': primary_trend if primary_trend in ['up', 'down'] else 'neutral',
                    'confidence': confidence,
                    'entry_allowed': confidence > 60 and primary_regime != 'choppy'
                },
                'session_quality': 'normal',  # Could be enhanced based on time
            
                # Keep the original data for reference
                'timeframe_alignment': {
                    'signals': {'BUY': buy_count, 'SELL': sell_count, 'HOLD': signals.count('HOLD')},
                    'trends': {'up': up_count, 'down': down_count, 'sideways': sideways_count}
                }
            }
        
        except Exception as e:
            self.logger.error(f"Error determining overall regime: {e}")
            return self._get_default_regime()
    
    def _get_default_regime(self) -> Dict:
        """Return default/fallback regime with complete structure"""
        return {
            'primary_regime': 'choppy',
            'confidence': 30,
            'supporting_factors': ['Insufficient or invalid data for analysis'],
            'trade_recommendation': False,
            'risk_level': 'high',
            'trend_details': {
                'primary_trend': 'unknown',
                'alignment_score': 0,
                'is_aligned': False,
                'has_conflict': True,
                'trends_by_timeframe': {},
                'higher_tf_agreement': False,
                'trend_strength': 'weak',
                'fast_vs_slow': 'unknown'
            },
            'volatility_details': {
                'volatility_regime': 'unknown',
                'range_percent': 0,
                'is_expanding': False,
                'is_contracting': False,
                'intraday_atr': 0
            },
            'momentum_details': {
                'average_momentum_score': 50,
                'momentum_state': 'neutral',
                'bullish_indicators': 0,
                'bearish_indicators': 0,
                'indicator_bias': 'neutral',
                'divergence_present': False,
                'momentum_quality': 'poor'
            },
            'scalping_bias': {
                'bias': 'neutral',
                'confidence': 0,
                'entry_allowed': False
            },
            'session_quality': 'unknown',
            'timeframe_analysis': {},
            'timeframe_alignment': {
                'signals': {'BUY': 0, 'SELL': 0, 'HOLD': 0},
                'trends': {'up': 0, 'down': 0, 'sideways': 0}
            }
        }
