# market_regime_hybrid.py
from market_regime import MarketRegime  # Your existing one
from market_regime_ohlc import OHLCRegimeDetector  # New OHLC one

class HybridRegimeDetector:
    """Runs both regime detections and compares"""
    
    def __init__(self):
        self.image_detector = MarketRegime()
        self.ohlc_detector = OHLCRegimeDetector()
        self.logger = logging.getLogger(__name__)
        
    def analyze_regime(self, timeframe_data: Dict = None, ohlc_data: Dict = None):
        """Run both analyses and log differences"""
        
        results = {}
        
        # Run existing image-based analysis if we have it
        if timeframe_data:
            results['image_based'] = self.image_detector.analyze_regime(timeframe_data)
            
        # Run new OHLC analysis if we have data
        if ohlc_data:
            results['ohlc_based'] = self.ohlc_detector.analyze_regime(ohlc_data)
            
        # Compare and log differences
        if 'image_based' in results and 'ohlc_based' in results:
            self._compare_results(results['image_based'], results['ohlc_based'])
            
        # For now, return image-based (no breaking change)
        # Later, you can switch to OHLC or combine them
        return results.get('image_based', results.get('ohlc_based'))
    
    def _compare_results(self, image_result: Dict, ohlc_result: Dict):
        """Log differences between methods"""
        if image_result['primary_regime'] != ohlc_result['primary_regime']:
            self.logger.warning(
                f"Regime mismatch! Image: {image_result['primary_regime']} "
                f"(conf: {image_result['confidence']}%), "
                f"OHLC: {ohlc_result['primary_regime']} "
                f"(conf: {ohlc_result['confidence']}%)"
            )
