import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def sanitize(obj):
    """Convert numpy/pandas objects to JSON-serializable Python types."""

    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, tuple):
        return [sanitize(v) for v in obj]
    return obj

PULLBACK_ZONE_SELL = (-0.2, 0.8, 0.3)  # lower, upper, sweet spot
PULLBACK_ZONE_BUY = (-0.8, 0.2, -0.3)
CHANNEL_DISTANCE_RANGE = (-0.2, 1.2)


def _safe_latest(series: pd.Series) -> Optional[float]:
    if series is None or series.empty:
        return None
    last_valid = series.dropna()
    if last_valid.empty:
        return None
    return float(last_valid.iloc[-1])


def _compute_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    if df.empty or len(df) < period + 1:
        return None
    highs = df["h"].astype(float)
    lows = df["l"].astype(float)
    closes = df["c"].astype(float)
    prev_close = closes.shift(1)
    tr = pd.concat([
        (highs - lows).abs(),
        (highs - prev_close).abs(),
        (lows - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.rolling(window=period, min_periods=period).mean()
    return _safe_latest(atr_series)


def _compute_ema(df: pd.DataFrame, period: int = 21) -> Optional[pd.Series]:
    if df.empty:
        return None
    return df["c"].astype(float).ewm(span=period, adjust=False).mean()


def _infer_base_bias(df: pd.DataFrame, atr_value: Optional[float]) -> str:
    ema_series = df.get("ema21")
    if ema_series is None or ema_series.isna().all():
        ema_series = _compute_ema(df)
    if ema_series is None or len(ema_series) < 2:
        return "HOLD"
    lookback = min(20, len(ema_series) - 1)
    slope = ema_series.iloc[-1] - ema_series.iloc[-(lookback + 1)]
    if atr_value and atr_value != 0:
        slope /= atr_value
    if slope > 0.05:
        return "BUY"
    if slope < -0.05:
        return "SELL"
    return "HOLD"


def _zone_confidence(value: float, lower: float, upper: float, sweet: float) -> float:
    if value < lower or value > upper:
        return 0.0
    half_range = (upper - lower) / 2 or 1
    return max(0.0, 1 - abs(value - sweet) / half_range)


def _pullback_component(df: pd.DataFrame, base_bias: str, atr_value: Optional[float]) -> Dict:
    tags: List[str] = []
    if atr_value is None or atr_value == 0:
        return {
            "name": "pullback_to_mean",
            "signal": 0,
            "confidence": 0.0,
            "tags": ["missing_data"],
            "metrics": {},
        }

    ema_series = df.get("ema21")
    if ema_series is None or ema_series.isna().all():
        ema_series = _compute_ema(df)
        if ema_series is not None:
            tags.append("ema_computed")

    ema_last = _safe_latest(ema_series) if ema_series is not None else None
    close_last = _safe_latest(df.get("c"))
    vwap_last = _safe_latest(df.get("vwap"))

    if ema_last is None or close_last is None:
        return {
            "name": "pullback_to_mean",
            "signal": 0,
            "confidence": 0.0,
            "tags": ["missing_data"],
            "metrics": {},
        }

    z_ema21 = (close_last - ema_last) / atr_value
    metrics = {"z_ema21": z_ema21}
    if vwap_last is not None:
        metrics["z_vwap"] = (close_last - vwap_last) / atr_value

    signal = 0
    confidence = 0.0
    if base_bias == "SELL":
        low, high, sweet = PULLBACK_ZONE_SELL
        if low <= z_ema21 <= high:
            signal = -1
            confidence = _zone_confidence(z_ema21, low, high, sweet)
            tags.append("pullback_zone")
    elif base_bias == "BUY":
        low, high, sweet = PULLBACK_ZONE_BUY
        if low <= z_ema21 <= high:
            signal = 1
            confidence = _zone_confidence(z_ema21, low, high, sweet)
            tags.append("pullback_zone")

    return {
        "name": "pullback_to_mean",
        "signal": signal,
        "confidence": float(confidence),
        "tags": tags,
        "metrics": metrics,
    }


def _detect_pivots(df: pd.DataFrame, lookback: int = 3) -> Dict[str, List[Dict]]:
    pivots_high: List[Dict] = []
    pivots_low: List[Dict] = []
    highs = df["h"].values
    lows = df["l"].values
    for i in range(lookback, len(df) - lookback):
        window_highs = highs[i - lookback : i + lookback + 1]
        window_lows = lows[i - lookback : i + lookback + 1]
        if highs[i] == window_highs.max():
            pivots_high.append({"x": i, "y": highs[i]})
        if lows[i] == window_lows.min():
            pivots_low.append({"x": i, "y": lows[i]})
    return {"highs": pivots_high, "lows": pivots_low}


def _fit_line(points: List[Dict]) -> Optional[Dict[str, float]]:
    if len(points) < 2:
        return None
    xs = np.array([p["x"] for p in points], dtype=float)
    ys = np.array([p["y"] for p in points], dtype=float)
    if len(points) >= 3:
        m, b = np.polyfit(xs, ys, 1)
    else:
        x1, x2 = xs
        y1, y2 = ys
        if x2 == x1:
            return None
        m = (y2 - y1) / (x2 - x1)
        b = y1 - m * x1
    return {"m": float(m), "b": float(b)}


def _line_value(line: Dict[str, float], x: float) -> Optional[float]:
    if not line:
        return None
    return line["m"] * x + line["b"]


def _channel_component(df: pd.DataFrame, base_bias: str, atr_value: Optional[float]) -> Dict:
    if df.empty or atr_value is None or atr_value == 0:
        return {
            "name": "trend_channel",
            "signal": 0,
            "confidence": 0.0,
            "tags": ["missing_data"],
            "metrics": {},
        }

    subset = df.tail(80).reset_index(drop=True)
    pivots = _detect_pivots(subset)
    pivot_highs = pivots["highs"]
    pivot_lows = pivots["lows"]

    line_high = _fit_line(pivot_highs[-3:]) or _fit_line(pivot_highs[-2:])
    line_low = _fit_line(pivot_lows[-3:]) or _fit_line(pivot_lows[-2:])

    x_last = len(subset) - 1
    close_last = float(subset["c"].iloc[-1])

    upper_y = _line_value(line_high, x_last) if line_high else None
    lower_y = _line_value(line_low, x_last) if line_low else None

    metrics = {
        "upper_y": upper_y,
        "lower_y": lower_y,
        "pivot_highs": len(pivot_highs),
        "pivot_lows": len(pivot_lows),
    }

    if upper_y is None or lower_y is None:
        return {
            "name": "trend_channel",
            "signal": 0,
            "confidence": 0.0,
            "tags": ["insufficient_pivots"],
            "metrics": metrics,
        }

    dist_to_upper = (upper_y - close_last) / atr_value
    dist_to_lower = (close_last - lower_y) / atr_value

    metrics.update(
        {
            "dist_to_upper_atr": dist_to_upper,
            "dist_to_lower_atr": dist_to_lower,
        }
    )

    broken_up = close_last > upper_y + 0.2 * atr_value
    broken_down = close_last < lower_y - 0.2 * atr_value
    metrics.update({"broken_up": broken_up, "broken_down": broken_down})

    last_swing_high = pivot_highs[-1]["y"] if pivot_highs else None
    last_swing_low = pivot_lows[-1]["y"] if pivot_lows else None

    bos_ok = True
    if base_bias == "SELL" and last_swing_high is not None:
        bos_ok = close_last <= last_swing_high + 0.1 * atr_value
    elif base_bias == "BUY" and last_swing_low is not None:
        bos_ok = close_last >= last_swing_low - 0.1 * atr_value
    metrics["bos_ok"] = bos_ok

    tags: List[str] = []
    signal = 0
    confidence = 0.0
    trendline_ok = True
    vol_ok = True

    if base_bias == "SELL":
        trendline_ok = not broken_up
        metrics["trendline_ok"] = trendline_ok
        metrics["vol_ok"] = vol_ok
        gates_pass = bos_ok and trendline_ok and vol_ok

        if broken_down and gates_pass:
            if -1.0 <= dist_to_lower <= 0.0:
                signal = -1
                confidence = max(0.0, min(1.0, 1 - abs(dist_to_lower)))
                tags.append("trend_continuation")
            elif dist_to_lower < -1.0:
                tags.append("too_extended")
        elif trendline_ok and CHANNEL_DISTANCE_RANGE[0] <= dist_to_upper <= CHANNEL_DISTANCE_RANGE[1]:
            signal = -1
            confidence = _zone_confidence(dist_to_upper, CHANNEL_DISTANCE_RANGE[0], CHANNEL_DISTANCE_RANGE[1], 0.4)
            tags.append("near_upper_channel")
    elif base_bias == "BUY":
        trendline_ok = not broken_down
        metrics["trendline_ok"] = trendline_ok
        metrics["vol_ok"] = vol_ok
        gates_pass = bos_ok and trendline_ok and vol_ok

        if broken_up and gates_pass:
            if -1.0 <= dist_to_upper <= 0.0:
                signal = 1
                confidence = max(0.0, min(1.0, 1 - abs(dist_to_upper)))
                tags.append("trend_continuation")
            elif dist_to_upper < -1.0:
                tags.append("too_extended")
        elif trendline_ok and CHANNEL_DISTANCE_RANGE[0] <= dist_to_lower <= CHANNEL_DISTANCE_RANGE[1]:
            signal = 1
            confidence = _zone_confidence(dist_to_lower, CHANNEL_DISTANCE_RANGE[0], CHANNEL_DISTANCE_RANGE[1], 0.4)
            tags.append("near_lower_channel")
    else:
        metrics["trendline_ok"] = trendline_ok
        metrics["vol_ok"] = vol_ok

    return {
        "name": "trend_channel",
        "signal": signal,
        "confidence": float(confidence),
        "tags": tags,
        "metrics": metrics,
    }


def compute_confluence(
    ohlc5m: pd.DataFrame,
    base_signal: Optional[str] = None,
    market_state: Optional[Dict] = None,
) -> Dict:
    """Compute confluence score from 5m OHLCV + indicator data."""

    if ohlc5m is None:
        ohlc5m = pd.DataFrame()
    else:
        ohlc5m = ohlc5m.copy()

    if not set(["o", "h", "l", "c"]).issubset(ohlc5m.columns):
        return {"confluence": {"score": 0.0, "bias": "HOLD", "components": [], "gates": {}}}

    ohlc5m = ohlc5m.sort_values("ts") if "ts" in ohlc5m.columns else ohlc5m

    atr_value = _safe_latest(ohlc5m.get("atr"))
    if atr_value is None or atr_value == 0:
        atr_value = _compute_atr(ohlc5m)

    market_bias = market_state.get("signal") if market_state else None
    bias = base_signal or market_bias or _infer_base_bias(ohlc5m, atr_value)
    components = []

    pullback = _pullback_component(ohlc5m, bias, atr_value)
    trend_channel = _channel_component(ohlc5m, bias, atr_value)
    components.extend([sanitize(pullback), sanitize(trend_channel)])

    weights = {
        "pullback_to_mean": 1.0,
        "trend_channel": 1.2,
    }

    score = 0.0
    component_breakdown = []
    for comp in components:
        w = weights.get(comp["name"], 1.0)
        signal = comp.get("signal", 0)
        confidence = comp.get("confidence", 0)
        contribution = w * signal * confidence
        score += contribution
        component_breakdown.append(
            {
                "name": comp.get("name"),
                "signal": signal,
                "confidence": confidence,
                "weight": w,
                "contribution": contribution,
            }
        )

    confluence_bias = "HOLD"
    if score >= 1.0:
        confluence_bias = "BUY"
    elif score <= -1.0:
        confluence_bias = "SELL"

    trendline_ok = bool(trend_channel.get("metrics", {}).get("trendline_ok", True))
    bos_ok = bool(trend_channel.get("metrics", {}).get("bos_ok", True))
    vol_ok = bool(trend_channel.get("metrics", {}).get("vol_ok", True))
    gates = {
        "trendline_ok": bool(trendline_ok),
        "bos_ok": bool(bos_ok),
        "vol_ok": bool(vol_ok),
    }

    threshold = 1.0
    score_satisfies = abs(score) >= threshold
    gates_ok = all(gates.values())
    market_signal = (market_state or {}).get("signal") if market_state else None
    market_confidence = float((market_state or {}).get("confidence", 0)) if market_state else 0.0
    market_trend_strong = market_signal in {"BUY", "SELL"} and market_confidence >= 80

    trend_tags = trend_channel.get("tags", []) if isinstance(trend_channel, dict) else []
    continuation_allowed = "trend_continuation" in trend_tags and "too_extended" not in trend_tags

    trade_by_score = score_satisfies and gates_ok
    trade_by_continuation = (
        market_trend_strong
        and gates_ok
        and (score_satisfies or continuation_allowed)
    )

    trade_recommended = trade_by_score or trade_by_continuation

    confluence = {
        "score": float(score),
        "bias": confluence_bias,
        "components": components,
        "gates": gates,
        "trade_recommended": trade_recommended,
    }

    if trade_recommended:
        path = "continuation" if trade_by_continuation and not trade_by_score else "pullback"
        logger.info(
            "Confluence trade via %s path: score=%.3f, gates_ok=%s, continuation=%s, components=%s",
            path,
            score,
            gates_ok,
            continuation_allowed,
            component_breakdown,
        )
    else:
        veto_reasons = []
        if not gates_ok:
            veto_reasons.append("gates_failed")
        if not score_satisfies:
            veto_reasons.append("score_below_threshold")
        if "too_extended" in trend_tags:
            veto_reasons.append("too_extended")
        logger.info(
            "Confluence vetoed: reasons=%s, score=%.3f, gates=%s, continuation_allowed=%s, bias=%s, market_signal=%s, components=%s",
            ",".join(veto_reasons) or "unknown",
            score,
            gates,
            continuation_allowed,
            bias,
            market_signal,
            component_breakdown,
        )

    return {"confluence": sanitize(confluence)}
