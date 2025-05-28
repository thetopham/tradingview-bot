# market_regime_hybrid.py
from market_regime import MarketRegime
from market_regime_ohlc import OHLCRegimeDetector
import logging
from typing import Dict, Optional, List
import statistics

class HybridRegimeDetector:
    """Combines image-based and OHLC regime detection for superior accuracy"""
    
    def __init__(self):
        self.image_detector = MarketRegime()
        self.ohlc_detector = OHLCRegimeDetector()
        self.logger = logging.getLogger(__name__)
        
    def analyze_regime(self, timeframe_data: Dict = None, ohlc_data: Dict = None) -> Dict:
        """
        Run both analyses and intelligently combine results
        
        Args:
            timeframe_data: Chart analysis data (for image-based)
            ohlc_data: Raw OHLC arrays (for OHLC-based)
        """
        
        results = {}
        
        # Run available analyses
        if timeframe_data:
            try:
                results['image_based'] = self.image_detector.analyze_regime(timeframe_data)
                self.logger.info(f"Image analysis: {results['image_based']['primary_regime']} "
                               f"(conf: {results['image_based']['confidence']}%)")
            except Exception as e:
                self.logger.error(f"Image analysis failed: {e}")
                
        if ohlc_data:
            try:
                results['ohlc_based'] = self.ohlc_detector.analyze_regime(ohlc_data)
                self.logger.info(f"OHLC analysis: {results['ohlc_based']['primary_regime']} "
                               f"(conf: {results['ohlc_based']['confidence']}%)")
            except Exception as e:
                self.logger.error(f"OHLC analysis failed: {e}")
        
        # If we have both, combine them intelligently
        if len(results) == 2:
            return self._combine_analyses(results['image_based'], results['ohlc_based'])
        elif 'ohlc_based' in results:
            return results['ohlc_based']
        elif 'image_based' in results:
            return results['image_based']
        else:
            return self._get_fallback_regime("No analysis available")
    
    def _combine_analyses(self, image_result: Dict, ohlc_result: Dict) -> Dict:
        """Intelligently combine both analysis results"""
        
        # If they agree, boost confidence
        if image_result['primary_regime'] == ohlc_result['primary_regime']:
            combined_confidence = min(
                95,
                (image_result['confidence'] + ohlc_result['confidence']) / 2 + 10
            )
            
            self.logger.info(f"✅ Analyses agree on {image_result['primary_regime']} regime")
            
            # Merge supporting factors
            combined_factors = list(set(
                image_result.get('supporting_factors', []) + 
                ohlc_result.get('supporting_factors', [])
            ))[:5]  # Keep top 5
            
            # Use image-based structure but enhance with OHLC data
            combined = image_result.copy()
            combined['confidence'] = combined_confidence
            combined['supporting_factors'] = combined_factors
            combined['analysis_method'] = 'hybrid_consensus'
            
            # Enhance trend details with OHLC precision
            if 'trend_details' in combined and 'timeframe_analysis' in ohlc_result:
                combined['trend_details']['ohlc_signals'] = {
                    tf: data.get('signal', 'HOLD')
                    for tf, data in ohlc_result['timeframe_analysis'].items()
                }
            
            return combined
            
        else:
            # They disagree - need to arbitrate
            return self._arbitrate_disagreement(image_result, ohlc_result)
    
    def _arbitrate_disagreement(self, image_result: Dict, ohlc_result: Dict) -> Dict:
        """Handle cases where analyses disagree"""
        
        self.logger.warning(f"⚠️ Regime disagreement: Image={image_result['primary_regime']} "
                          f"vs OHLC={ohlc_result['primary_regime']}")
        
        # Decision factors
        factors = []
        
        # 1. Confidence-based selection
        if abs(image_result['confidence'] - ohlc_result['confidence']) > 20:
            # One is much more confident
            primary = image_result if image_result['confidence'] > ohlc_result['confidence'] else ohlc_result
            factors.append(f"Higher confidence analysis selected ({primary['confidence']}%)")
        
        # 2. Check trend alignment scores
        else:
            image_alignment = image_result.get('trend_details', {}).get('alignment_score', 0)
            ohlc_alignment = ohlc_result.get('trend_details', {}).get('alignment_score', 0)
            
            if abs(image_alignment - ohlc_alignment) > 15:
                primary = image_result if image_alignment > ohlc_alignment else ohlc_result
                factors.append(f"Better trend alignment ({max(image_alignment, ohlc_alignment):.0f}%)")
            else:
                # 3. Prefer OHLC for ranging/choppy, image for trending
                if ohlc_result['primary_regime'] in ['ranging', 'choppy']:
                    primary = ohlc_result
                    factors.append("OHLC preferred for range detection")
                else:
                    primary = image_result
                    factors.append("Image analysis preferred for trend detection")
        
        # Create combined result with reduced confidence
        combined = primary.copy()
        combined['confidence'] = max(40, primary['confidence'] - 15)  # Reduce confidence due to disagreement
        combined['supporting_factors'] = factors + primary.get('supporting_factors', [])[:3]
        combined['analysis_method'] = 'hybrid_arbitrated'
        combined['disagreement_note'] = (
            f"Image: {image_result['primary_regime']} ({image_result['confidence']}%) vs "
            f"OHLC: {ohlc_result['primary_regime']} ({ohlc_result['confidence']}%)"
        )
        
        # Add cross-validation data
        combined['cross_validation'] = {
            'image_regime': image_result['primary_regime'],
            'image_confidence': image_result['confidence'],
            'ohlc_regime': ohlc_result['primary_regime'],
            'ohlc_confidence': ohlc_result['confidence'],
            'agreement': False
        }
        
        return combined
    
    def _get_fallback_regime(self, reason: str) -> Dict:
        """Fallback when no analysis is available"""
        return {
            'primary_regime': 'choppy',
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
            'analysis_method': 'fallback'
        }
