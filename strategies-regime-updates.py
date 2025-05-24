# Updates for strategies.py - Add these functions at the top after imports

from market_regime import MarketRegime
from api import get_market_conditions_summary

def get_regime_adjusted_params(base_sl_points: float, base_tp_points: list, regime_data: dict = None) -> tuple:
    """
    Adjust stop loss and take profit based on market regime
    
    Args:
        base_sl_points: Base stop loss in points
        base_tp_points: Base take profit levels in points
        regime_data: Market regime data (if None, will fetch current)
        
    Returns:
        Tuple of (adjusted_sl_points, adjusted_tp_points)
    """
    # Get current regime if not provided
    if regime_data is None:
        try:
            summary = get_market_conditions_summary()
            regime = summary.get('regime', 'choppy')
            volatility = summary.get('volatility', 'medium')
        except:
            regime = 'choppy'
            volatility = 'medium'
    else:
        regime = regime_data.get('primary_regime', 'choppy')
        volatility = regime_data.get('volatility_details', {}).get('volatility_regime', 'medium')
    
    # Regime-based adjustments
    sl_multiplier = 1.0
    tp_multiplier = 1.0
    
    if regime == 'trending_up' or regime == 'trending_down':
        # In trending markets, give trades more room
        sl_multiplier = 1.2
        tp_multiplier = 1.5
    elif regime == 'ranging':
        # In ranging markets, tighter stops and targets
        sl_multiplier = 0.8
        tp_multiplier = 0.8
    elif regime == 'choppy':
        # In choppy markets, very tight stops
        sl_multiplier = 0.6
        tp_multiplier = 0.6
    elif regime == 'breakout':
        # In breakout, wider stops but bigger targets
        sl_multiplier = 1.3
        tp_multiplier = 2.0
    
    # Volatility adjustments
    if volatility == 'high':
        sl_multiplier *= 1.2
        tp_multiplier *= 1.2
    elif volatility == 'low':
        sl_multiplier *= 0.9
        tp_multiplier *= 0.9
    
    # Apply adjustments
    adjusted_sl = base_sl_points * sl_multiplier
    adjusted_tp = [tp * tp_multiplier for tp in base_tp_points]
    
    logging.info(f"Regime adjustments ({regime}/{volatility}): "
                f"SL {base_sl_points} -> {adjusted_sl:.1f}, "
                f"TP {base_tp_points} -> {[f'{tp:.1f}' for tp in adjusted_tp]}")
    
    return adjusted_sl, adjusted_tp

# Update the run_bracket function
def run_bracket(acct_id, sym, sig, size, alert, ai_decision_id=None):
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1
    exit_side = 1 - side
    pos = [p for p in search_pos(acct_id) if p["contractId"] == cid]

    if any((side == 0 and p["type"] == 1) or (side == 1 and p["type"] == 2) for p in pos):
        return  # skip same

    if any((side == 0 and p["type"] == 2) or (side == 1 and p["type"] == 1) for p in pos):
        success = flatten_contract(acct_id, cid, timeout=10)
        if not success:
            return  # Could not flatten contract

    # Get current market regime
    try:
        market_summary = get_market_conditions_summary()
        regime_data = market_summary
    except:
        regime_data = None

    ent = place_market(acct_id, cid, side, size)
    oid = ent["orderId"]
    entry_time = datetime.now(CT)

    price = None
    for _ in range(12):
        trades = [t for t in search_trades(acct_id, datetime.now(CT)-timedelta(minutes=5)) if t["orderId"]==oid]
        tot = sum(t["size"] for t in trades)
        if tot:
            price = sum(t["price"]*t["size"] for t in trades)/tot
            break
        price = ent.get("fillPrice")
        if price is not None:
            break
        time.sleep(1)

    if price is None:
        return  # No fill price

    # Get regime-adjusted parameters
    adjusted_sl, adjusted_tp = get_regime_adjusted_params(
        STOP_LOSS_POINTS, 
        TP_POINTS,
        regime_data
    )

    slp = price - adjusted_sl if side==0 else price + adjusted_sl
    sl  = place_stop(acct_id, cid, exit_side, size, slp)
    sl_id = sl["orderId"]

    tp_ids = []
    n = len(adjusted_tp)
    base = size // n
    rem = size - base * n
    slices = [base] * n
    slices[-1] += rem
    
    for pts, amt in zip(adjusted_tp, slices):
        px = price + pts if side == 0 else price - pts
        r = place_limit(acct_id, cid, exit_side, amt, px)
        tp_ids.append(r["orderId"])

    # Track with regime info
    track_trade(
        acct_id=acct_id,
        cid=cid,
        entry_time=entry_time.timestamp(),
        ai_decision_id=ai_decision_id,
        strategy="bracket",
        sig=sig,
        size=size,
        order_id=oid,
        alert=alert,
        account=acct_id,
        symbol=sym,
        sl_id=sl_id,
        tp_ids=tp_ids,
        trades=None,
        regime=regime_data.get('regime', 'unknown') if regime_data else 'unknown'
    )

    check_for_phantom_orders(acct_id, cid)

# Update the run_brackmod function similarly
def run_brackmod(acct_id, sym, sig, size, alert, ai_decision_id=None):
    cid = get_contract(sym)
    side = 0 if sig == "BUY" else 1
    exit_side = 1 - side
    pos = [p for p in search_pos(acct_id) if p["contractId"] == cid]
    if any((side == 0 and p["type"] == 1) or (side == 1 and p["type"] == 2) for p in pos):
        return  # skip same
    if any((side == 0 and p["type"] == 2) or (side == 1 and p["type"] == 1) for p in pos):
        success = flatten_contract(acct_id, cid, timeout=10)
        if not success:
            return  # Could not flatten contract

    # Get current market regime
    try:
        market_summary = get_market_conditions_summary()
        regime_data = market_summary
    except:
        regime_data = None

    ent = place_market(acct_id, cid, side, size)
    oid = ent["orderId"]
    entry_time = datetime.now(CT)
    price = None
    for _ in range(12):
        trades = [t for t in search_trades(acct_id, datetime.now(CT) - timedelta(minutes=5)) if t["orderId"] == oid]
        tot = sum(t["size"] for t in trades)
        if tot:
            price = sum(t["price"] * t["size"] for t in trades) / tot
            break
        price = ent.get("fillPrice")
        if price is not None:
            break
        time.sleep(1)
    if price is None:
        return  # No fill price

    # Base brackmod parameters
    BASE_STOP_LOSS_POINTS = 5.75
    BASE_TP_POINTS = [2.5, 5.0]
    
    # Get regime-adjusted parameters
    adjusted_sl, adjusted_tp = get_regime_adjusted_params(
        BASE_STOP_LOSS_POINTS, 
        BASE_TP_POINTS,
        regime_data
    )
    
    slp = price - adjusted_sl if side == 0 else price + adjusted_sl
    sl = place_stop(acct_id, cid, exit_side, size, slp)
    sl_id = sl["orderId"]
    
    # Brackmod uses fixed position sizes for TPs
    slices = [2, 1] if size == 3 else [1, 1]  # Adjust if size != 3
    tp_ids = []
    for pts, amt in zip(adjusted_tp, slices[:len(adjusted_tp)]):
        px = price + pts if side == 0 else price - pts
        r = place_limit(acct_id, cid, exit_side, amt, px)
        tp_ids.append(r["orderId"])

    track_trade(
        acct_id=acct_id,
        cid=cid,
        entry_time=entry_time.timestamp(),
        ai_decision_id=ai_decision_id,
        strategy="brackmod",
        sig=sig,
        size=size,
        order_id=oid,
        alert=alert,
        account=acct_id,
        symbol=sym,
        sl_id=sl_id,
        tp_ids=tp_ids,
        trades=None,
        regime=regime_data.get('regime', 'unknown') if regime_data else 'unknown'
    )

    check_for_phantom_orders(acct_id, cid)

# Note: You'll also need to update the track_trade function in signalr_listener.py 
# to accept the regime parameter