# Add these functions to api.py

from market_regime import MarketRegime

# Create global market regime analyzer
market_regime_analyzer = MarketRegime()

def fetch_multi_timeframe_analysis(n8n_base_url: str, timeframes: List[str] = None) -> Dict:
    """
    Fetch chart analysis from multiple timeframes via n8n
    
    Args:
        n8n_base_url: Base URL for n8n instance
        timeframes: List of timeframes to fetch (default: ['1m', '5m', '15m', '30m', '1h'])
    
    Returns:
        Dict with timeframe data and regime analysis
    """
    if timeframes is None:
        timeframes = ['1m', '5m', '15m', '30m', '1h']
    
    timeframe_data = {}
    
    for tf in timeframes:
        try:
            # Construct n8n webhook URL for each timeframe
            webhook_url = f"{n8n_base_url}/webhook/{tf}"
            
            # Call n8n workflow for chart analysis
            response = session.post(webhook_url, json={}, timeout=30)
            response.raise_for_status()
            
            # Parse response
            data = response.json()
            if isinstance(data, str):
                # Sometimes n8n returns stringified JSON
                data = json.loads(data)
            
            timeframe_data[tf] = data
            logging.info(f"Fetched {tf} analysis: signal={data.get('signal')}, trend={data.get('trend')}")
            
        except Exception as e:
            logging.error(f"Failed to fetch {tf} analysis: {e}")
            timeframe_data[tf] = {}
    
    # Analyze market regime
    regime_analysis = market_regime_analyzer.analyze_regime(timeframe_data)
    
    return {
        'timeframe_data': timeframe_data,
        'regime_analysis': regime_analysis,
        'timestamp': datetime.now(CT).isoformat()
    }

def ai_trade_decision_with_regime(account, strat, sig, sym, size, alert, ai_url):
    """
    Enhanced AI trade decision that includes market regime analysis
    """
    # First, get market regime analysis
    n8n_base_url = ai_url.split('/webhook/')[0]  # Extract base URL
    market_analysis = fetch_multi_timeframe_analysis(n8n_base_url)
    
    regime = market_analysis['regime_analysis']
    regime_rules = market_regime_analyzer.get_regime_trading_rules(regime['primary_regime'])
    
    # Check if trading is recommended in this regime
    if not regime['trade_recommendation']:
        logging.warning(f"Trading not recommended in {regime['primary_regime']} regime. Blocking trade.")
        return {
            "strategy": strat,
            "signal": "HOLD",
            "account": account,
            "reason": f"Market regime ({regime['primary_regime']}) not suitable for trading. {', '.join(regime['supporting_factors'])}",
            "regime": regime['primary_regime'],
            "error": False
        }
    
    # Check if signal aligns with regime
    if regime_rules['avoid_signal'] == 'BOTH' or (regime_rules['avoid_signal'] and sig == regime_rules['avoid_signal']):
        logging.warning(f"Signal {sig} conflicts with {regime['primary_regime']} regime preferences")
        return {
            "strategy": strat,
            "signal": "HOLD",
            "account": account,
            "reason": f"{sig} signal not recommended in {regime['primary_regime']} regime",
            "regime": regime['primary_regime'],
            "error": False
        }
    
    # Prepare enhanced payload for AI
    payload = {
        "account": account,
        "strategy": strat,
        "signal": sig,
        "symbol": sym,
        "size": size,
        "alert": alert,
        "market_analysis": {
            "regime": regime['primary_regime'],
            "confidence": regime['confidence'],
            "supporting_factors": regime['supporting_factors'],
            "risk_level": regime['risk_level'],
            "trend_details": regime['trend_details'],
            "volatility_details": regime['volatility_details'],
            "momentum_details": regime['momentum_details']
        },
        "regime_rules": regime_rules,
        "timeframe_signals": {
            tf: data.get('signal', 'HOLD') 
            for tf, data in market_analysis['timeframe_data'].items()
        }
    }
    
    try:
        resp = session.post(ai_url, json=payload, timeout=240)
        resp.raise_for_status()
        
        try:
            data = resp.json()
        except Exception as e:
            logging.error(f"AI response not valid JSON: {resp.text}")
            return {
                "strategy": strat,
                "signal": "HOLD",
                "account": account,
                "reason": f"AI response parsing error: {str(e)}",
                "regime": regime['primary_regime'],
                "error": True
            }
        
        # Add regime info to response
        data['regime'] = regime['primary_regime']
        data['regime_confidence'] = regime['confidence']
        
        # Apply position sizing based on regime
        if 'size' in data and regime_rules['max_position_size'] > 0:
            data['size'] = min(data['size'], regime_rules['max_position_size'])
        
        return data
        
    except Exception as e:
        logging.error(f"AI error with regime analysis: {str(e)}")
        return {
            "strategy": strat,
            "signal": "HOLD",
            "account": account,
            "reason": f"AI error: {str(e)}",
            "regime": regime['primary_regime'],
            "error": True
        }

def get_market_conditions_summary() -> Dict:
    """
    Get a summary of current market conditions for logging
    """
    # This could be called periodically to log market state
    market_analysis = fetch_multi_timeframe_analysis(config.get('N8N_AI_URL', '').split('/webhook/')[0])
    regime = market_analysis['regime_analysis']
    
    summary = {
        'timestamp': datetime.now(CT).isoformat(),
        'regime': regime['primary_regime'],
        'confidence': regime['confidence'],
        'trade_recommended': regime['trade_recommendation'],
        'risk_level': regime['risk_level'],
        'key_factors': regime['supporting_factors'][:3],  # Top 3 factors
        'trend_alignment': regime['trend_details']['alignment_score'],
        'volatility': regime['volatility_details']['volatility_regime']
    }
    
    logging.info(f"Market Conditions: {summary}")
    return summary