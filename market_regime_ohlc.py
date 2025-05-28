import numpy as np
import logging
from typing import Dict, List, Optional, Tuple

def _analyze_single_timeframe(self, data: dict[str, List], timeframe: str) -> Dict:
    """Analyze a single timeframe's OHLC data - ENHANCED WITH INDICATORS"""
    
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
    
    # Recent vs historical analysis - different windows for different timeframes
    if timeframe == '1m':
        recent_bars = 15
        medium_bars = 30
    elif timeframe == '5m':
        recent_bars = 10
        medium_bars = 20
    elif timeframe == '15m':
        recent_bars = 8
        medium_bars = 16
    else:  # 30m or higher
        recent_bars = 6
        medium_bars = 12
    
    # Ensure we have enough data
    if len(closes) < recent_bars:
        recent_bars = len(closes)
        medium_bars = min(len(closes), medium_bars)
    
    recent_closes = closes[-recent_bars:]
    recent_highs = highs[-recent_bars:]
    recent_lows = lows[-recent_bars:]
    recent_opens = opens[-recent_bars:]
    
    # 1. Enhanced Trend Detection with indicators
    trend = self._calculate_trend(recent_closes, recent_highs, recent_lows)
    
    # Confirm/adjust trend with indicators
    recent_rsi = rsi_values[-1] if len(rsi_values) > 0 else 50
    recent_macd = macd_hist[-1] if len(macd_hist) > 0 else 0
    recent_fisher = fisher_values[-1] if len(fisher_values) > 0 else 0
    
    # Check for divergences
    divergence_detected = False
    if trend['direction'] == 'up':
        # Bearish divergence check
        if recent_rsi < 40 or (recent_macd < 0 and abs(recent_macd) > np.std(macd_hist[-20:])):
            trend['strength'] = 'weak'
            divergence_detected = True
    elif trend['direction'] == 'down':
        # Bullish divergence check
        if recent_rsi > 60 or (recent_macd > 0 and recent_macd > np.std(macd_hist[-20:])):
            trend['strength'] = 'weak'
            divergence_detected = True
    
    # 2. Enhanced Volatility with ATR and Bollinger Bands
    if len(atr_values) > 0 and atr_values[-1] > 0:
        current_atr = atr_values[-1]
        avg_atr = np.mean(atr_values[-20:]) if len(atr_values) >= 20 else np.mean(atr_values)
        
        # Check Bollinger Band width
        bb_width = (bb_upper[-1] - bb_lower[-1]) if len(bb_upper) > 0 else 0
        bb_width_avg = np.mean(bb_upper[-10:] - bb_lower[-10:]) if len(bb_upper) >= 10 else bb_width
        
        volatility = {
            'level': 'high' if current_atr > avg_atr * 1.3 else 'low' if current_atr < avg_atr * 0.7 else 'medium',
            'atr': float(current_atr),
            'average_range': float(current_atr),
            'expanding': current_atr > avg_atr and bb_width > bb_width_avg,
            'recent_range': float(highs[-1] - lows[-1]),
            'bb_width': float(bb_width),
            'bb_squeeze': bb_width < bb_width_avg * 0.8  # Bollinger squeeze
        }
    else:
        # Fallback to basic calculation
        volatility = self._calculate_volatility(recent_highs, recent_lows, recent_closes)
    
    # 3. Enhanced Momentum with multiple indicators
    momentum = self._calculate_enhanced_momentum(
        closes, rsi_values, macd_hist, fisher_values, vzo_values, phobos_values
    )
    
    # 4. Support/Resistance with Bollinger Bands
    support_resistance = self._find_support_resistance(highs, lows, closes)
    
    # Add Bollinger Bands as dynamic S/R
    if len(bb_upper) > 0:
        support_resistance['dynamic_resistance'] = float(bb_upper[-1])
        support_resistance['dynamic_support'] = float(bb_lower[-1])
        support_resistance['bb_middle'] = float(bb_middle[-1])
    
    # 5. Enhanced Range Detection
    is_ranging, range_bounds = self._detect_range(recent_highs, recent_lows, recent_closes)
    
    # Check if we're in a Bollinger Band squeeze (often precedes breakout)
    if volatility.get('bb_squeeze', False):
        is_ranging = True
        range_bounds['squeeze_detected'] = True
    
    # 6. Pattern Detection with indicator confirmation
    patterns = self._detect_patterns(opens, highs, lows, closes)
    
    # Add indicator-based patterns
    if divergence_detected:
        if trend['direction'] == 'up':
            patterns.append('bearish_divergence')
        else:
            patterns.append('bullish_divergence')
    
    # Fisher extremes
    if len(fisher_values) > 0:
        if fisher_values[-1] > 2:
            patterns.append('fisher_extreme_high')
        elif fisher_values[-1] < -2:
            patterns.append('fisher_extreme_low')
    
    # 7. Generate signal with all information
    signal_info = self._generate_enhanced_signal(
        trend, momentum, volatility, is_ranging, 
        recent_closes[-1], support_resistance, 
        {'rsi': recent_rsi, 'macd': recent_macd, 'fisher': recent_fisher, 'vzo': vzo_values[-1] if len(vzo_values) > 0 else 0}
    )
    
    # Build comprehensive analysis result
    return {
        'trend': trend['direction'],
        'trend_strength': trend['strength'],
        'trend_quality': trend['quality'],
        'trend_metrics': {
            'slope': trend['slope'],
            'slope_degrees': trend['slope_degrees'],
            'higher_highs': trend['higher_highs'],
            'lower_lows': trend['lower_lows']
        },
        'volatility': volatility['level'],
        'atr': volatility['atr'],
        'volatility_expanding': volatility.get('expanding', False),
        'bb_squeeze': volatility.get('bb_squeeze', False),
        'momentum': momentum['state'],
        'momentum_value': momentum['value'],
        'momentum_score': momentum.get('indicator_score', 0),
        'support': support_resistance['support'],
        'resistance': support_resistance['resistance'],
        'dynamic_levels': {
            'bb_upper': support_resistance.get('dynamic_resistance'),
            'bb_middle': support_resistance.get('bb_middle'),
            'bb_lower': support_resistance.get('dynamic_support')
        },
        'is_ranging': is_ranging,
        'range_bounds': range_bounds,
        'patterns': patterns,
        'signal': signal_info['signal'],
        'signal_confidence': signal_info['confidence'],
        'signal_reasons': signal_info['reasons'],
        'indicators': {
            'rsi': float(recent_rsi),
            'macd_hist': float(recent_macd),
            'fisher': float(recent_fisher),
            'vzo': float(vzo_values[-1]) if len(vzo_values) > 0 else 0,
            'phobos': float(phobos_values[-1]) if len(phobos_values) > 0 else 0,
            'stoch_k': float(stoch_k[-1]) if len(stoch_k) > 0 else 50
        },
        'current_price': float(closes[-1]),
        'price_vs_bb': 'above' if closes[-1] > bb_upper[-1] else 'below' if closes[-1] < bb_lower[-1] else 'inside',
        'recent_high': float(recent_highs.max()),
        'recent_low': float(recent_lows.min()),
        'recent_range': float(recent_highs.max() - recent_lows.min()),
        'timestamp': timeframe
    }

def _calculate_enhanced_momentum(self, closes, rsi, macd_hist, fisher, vzo, phobos):
    """Calculate momentum using multiple indicators with proper weighting"""
    
    # Price momentum (existing)
    mom_5 = closes[-1] - closes[-5] if len(closes) >= 5 else 0
    mom_10 = closes[-1] - closes[-10] if len(closes) >= 10 else 0
    mom_20 = closes[-1] - closes[-20] if len(closes) >= 20 else 0
    
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
    if len(macd_hist) >= 5:
        current_macd = macd_hist[-1]
        prev_macd = macd_hist[-5]
        
        if current_macd > 0:
            if current_macd > prev_macd:  # Accelerating up
                momentum_score += 12.5
            else:
                momentum_score += 6.25
        elif current_macd < 0:
            if current_macd < prev_macd:  # Accelerating down
                momentum_score -= 12.5
            else:
                momentum_score -= 6.25
    
    # VZO momentum (Volume Zone Oscillator) (weight: 20%)
    if len(vzo) > 0 and vzo[-1] != 0:
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
    if len(phobos) > 0 and phobos[-1] != 0:
        current_phobos = phobos[-1]
        if current_phobos > 0.5:
            momentum_score += 7.5
        elif current_phobos < -0.5:
            momentum_score -= 7.5
    
    # Determine state based on combined score and price movement
    if momentum_score >= 70 and mom_5 > 3:
        state = 'accelerating'
    elif momentum_score <= 30 and mom_5 < -3:
        state = 'decelerating'
    elif abs(mom_5) < 2 and 40 <= momentum_score <= 60:
        state = 'flat'
    elif momentum_score > 60:
        state = 'steady_bullish'
    elif momentum_score < 40:
        state = 'steady_bearish'
    else:
        state = 'steady'
    
    return {
        'state': state,
        'value': float(mom_5),
        'indicator_score': float(momentum_score),
        '5_bar': float(mom_5),
        '10_bar': float(mom_10),
        '20_bar': float(mom_20),
        'acceleration': float(mom_5 - mom_10) if len(closes) >= 10 else 0
    }

def _generate_enhanced_signal(self, trend, momentum, volatility, is_ranging, 
                            current_price, sr, indicators):
    """Generate trading signal with confidence and reasoning"""
    
    signal = "HOLD"
    confidence = 50
    reasons = []
    
    # Range-bound strategy
    if is_ranging:
        range_position = (current_price - sr['nearest_support']) / (sr['nearest_resistance'] - sr['nearest_support']) \
                        if sr['nearest_resistance'] > sr['nearest_support'] else 0.5
        
        if range_position <= 0.2:  # Near support
            if indicators['rsi'] < 40 and indicators['vzo'] < -20:
                signal = "BUY"
                confidence = 75
                reasons.append("At range support with oversold indicators")
            elif momentum['state'] in ['flat', 'steady_bullish']:
                signal = "BUY"
                confidence = 65
                reasons.append("At range support")
        
        elif range_position >= 0.8:  # Near resistance
            if indicators['rsi'] > 60 and indicators['vzo'] > 20:
                signal = "SELL"
                confidence = 75
                reasons.append("At range resistance with overbought indicators")
            elif momentum['state'] in ['flat', 'steady_bearish']:
                signal = "SELL"
                confidence = 65
                reasons.append("At range resistance")
        else:
            reasons.append("Middle of range - no edge")
    
    # Trend following with indicator confirmation
    elif trend['direction'] == 'up' and trend['strength'] in ['strong', 'moderate']:
        if momentum['indicator_score'] > 60:
            if volatility.get('bb_squeeze'):
                signal = "BUY"
                confidence = 80
                reasons.append("Uptrend with squeeze breakout potential")
            elif current_price <= sr.get('bb_middle', current_price):
                signal = "BUY"
                confidence = 75
                reasons.append("Uptrend pullback to mid-BB")
            elif momentum['state'] == 'accelerating':
                signal = "BUY"
                confidence = 70
                reasons.append("Uptrend with accelerating momentum")
            else:
                signal = "BUY"
                confidence = 65
                reasons.append("Uptrend continuation")
        else:
            reasons.append("Uptrend but weak momentum - waiting")
    
    elif trend['direction'] == 'down' and trend['strength'] in ['strong', 'moderate']:
        if momentum['indicator_score'] < 40:
            if volatility.get('bb_squeeze'):
                signal = "SELL"
                confidence = 80
                reasons.append("Downtrend with squeeze breakout potential")
            elif current_price >= sr.get('bb_middle', current_price):
                signal = "SELL"
                confidence = 75
                reasons.append("Downtrend pullback to mid-BB")
            elif momentum['state'] == 'decelerating':
                signal = "SELL"
                confidence = 70
                reasons.append("Downtrend with accelerating momentum")
            else:
                signal = "SELL"
                confidence = 65
                reasons.append("Downtrend continuation")
        else:
            reasons.append("Downtrend but weak momentum - waiting")
    
    # Sideways/weak trend
    else:
        if indicators['fisher'] > 2:
            signal = "SELL"
            confidence = 60
            reasons.append("Fisher extreme - potential reversal")
        elif indicators['fisher'] < -2:
            signal = "BUY"
            confidence = 60
            reasons.append("Fisher extreme - potential reversal")
        else:
            reasons.append("No clear trend or range - staying out")
    
    return {
        'signal': signal,
        'confidence': confidence,
        'reasons': reasons
    }
