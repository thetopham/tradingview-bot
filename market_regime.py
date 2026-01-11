'''
import json
import logging
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from supabase import create_client


@dataclass
class RegimeSettings:
    # NOTE: state_path is now a *format string*.
    # Example: "./market_state_{symbol}_{timeframe}.json"
    state_path: str

    refresh_seconds: int
    trend_lookback: int
    vol_lookback: int
    confirmations: int
    default_timeframe: str
    table_map: Dict[str, str]
    er_threshold: float
    atr_ratio_threshold: float

    # New (but safe defaults)
    fast_trend_lookback: int
    impulse_tr_mult: float
    digestion_bars: int


_SUPABASE_CLIENT = None


def _get_settings() -> RegimeSettings:
    # IMPORTANT: include {symbol} and {timeframe} so multiple timeframes don't overwrite each other's state.
    # If you set MARKET_STATE_PATH yourself, set it like:
    #   MARKET_STATE_PATH=./market_state_{symbol}_{timeframe}.json
    state_path = os.getenv("MARKET_STATE_PATH", "./market_state_{symbol}_{timeframe}.json")

    refresh_seconds = int(os.getenv("REGIME_REFRESH_SECONDS", "20"))
    trend_lookback = int(os.getenv("REGIME_TREND_LOOKBACK", "30"))
    vol_lookback = int(os.getenv("REGIME_VOL_LOOKBACK", "120"))
    confirmations = int(os.getenv("REGIME_CONFIRMATIONS", "3"))
    default_timeframe = os.getenv("REGIME_DEFAULT_TIMEFRAME", "5m")
    table_map = {
        "5m": os.getenv("REGIME_TABLE_5M", "tv_datafeed_5m"),
        "15m": os.getenv("REGIME_TABLE_15M", "tv_datafeed_15m"),
        "30m": os.getenv("REGIME_TABLE_30M", "tv_datafeed_30m"),
    }

    # New knobs
    fast_trend_lookback = int(os.getenv("REGIME_FAST_TREND_LOOKBACK", str(max(10, trend_lookback // 3))))
    impulse_tr_mult = float(os.getenv("REGIME_IMPULSE_TR_MULT", "2.5"))
    digestion_bars = int(os.getenv("REGIME_DIGESTION_BARS", "8"))

    return RegimeSettings(
        state_path=state_path,
        refresh_seconds=refresh_seconds,
        trend_lookback=trend_lookback,
        vol_lookback=vol_lookback,
        confirmations=confirmations,
        default_timeframe=default_timeframe,
        table_map=table_map,
        er_threshold=float(os.getenv("REGIME_ER_THRESHOLD", "0.4")),
        atr_ratio_threshold=float(os.getenv("REGIME_ATR_RATIO_THRESHOLD", "1.5")),
        fast_trend_lookback=fast_trend_lookback,
        impulse_tr_mult=impulse_tr_mult,
        digestion_bars=digestion_bars,
    )


def _get_supabase_client():
    global _SUPABASE_CLIENT
    if _SUPABASE_CLIENT is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise RuntimeError("Supabase credentials are not configured")
        _SUPABASE_CLIENT = create_client(url, key)
    return _SUPABASE_CLIENT


def _normalize_symbol(symbol: Optional[str]) -> str:
    if not symbol:
        return "MES"
    upper = symbol.upper()
    if "MES" in upper:
        return "MES"
    return symbol


def _format_state_path(state_path_fmt: str, symbol: str, timeframe: str) -> str:
    """
    Resolve a per-(symbol,timeframe) state path. If the env var doesn't include placeholders,
    we fallback to a safe derived filename so timeframes don't overwrite each other.
    """
    try:
        rendered = state_path_fmt.format(symbol=symbol, timeframe=timeframe)
        # If the user passed a template without placeholders, format() returns same string.
        # Detect that and auto-suffix to avoid collisions.
        if rendered == state_path_fmt and ("{symbol}" not in state_path_fmt and "{timeframe}" not in state_path_fmt):
            base, ext = os.path.splitext(state_path_fmt)
            ext = ext or ".json"
            rendered = f"{base}_{symbol}_{timeframe}{ext}"
        return rendered
    except Exception:
        # Very defensive: if format string is invalid, fall back to a safe default.
        base, ext = os.path.splitext(state_path_fmt)
        ext = ext or ".json"
        base = base or "./market_state"
        return f"{base}_{symbol}_{timeframe}{ext}"


def _load_state(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        logging.warning("Failed to read market regime state (%s): %s", path, exc)
        return None


def _write_state(path: str, state: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    backup_path = f"{path}.bak"
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                existing = handle.read()
            with open(backup_path, "w", encoding="utf-8") as backup:
                backup.write(existing)
        except Exception as exc:
            logging.debug("Failed to write market regime backup: %s", exc)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def _fetch_bars(table: str, symbol: str, limit: int) -> List[Dict[str, Any]]:
    """
    Prefer OHLC+ATR. If your table doesn't have o/h/l, Supabase will return nulls; we handle that.
    """
    supabase = _get_supabase_client()
    result = (
        supabase.table(table)
        .select("ts,o,h,l,c,atr")
        .eq("symbol", symbol)
        .order("ts", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def _compute_efficiency_ratio(closes: List[float], lookback: int) -> Optional[float]:
    if len(closes) < lookback + 1:
        return None
    recent = closes[-(lookback + 1) :]
    change = abs(recent[-1] - recent[0])
    volatility = sum(abs(recent[i] - recent[i - 1]) for i in range(1, len(recent)))
    if volatility == 0:
        return 0.0
    return change / volatility


def _compute_atr_ratio(atrs: List[float], lookback: int) -> Optional[float]:
    if len(atrs) < lookback:
        return None
    recent = atrs[-lookback:]
    median_atr = statistics.median(recent)
    if median_atr == 0:
        return None
    current_atr = atrs[-1]
    return current_atr / median_atr


def _true_range(prev_close: float, high: Optional[float], low: Optional[float], close: float) -> float:
    # If we have high/low, use real TR. Otherwise, fallback to close-to-close move.
    if high is None or low is None:
        return abs(close - prev_close)
    return max(
        float(high) - float(low),
        abs(float(high) - prev_close),
        abs(float(low) - prev_close),
    )


def _derive_action(trend_dim: str, vol_dim: str, phase: str) -> Tuple[bool, str]:
    """
    Deterministic "what to do" mapping. Keep it tiny and opinionated.
    """
    if phase in ("impulse", "digestion"):
        # Post-impulse chop is where bots get shredded.
        return False, "cooldown"

    if trend_dim.startswith("trending") and vol_dim == "normal":
        return True, "normal"

    if trend_dim.startswith("trending") and vol_dim == "high":
        return True, "reduced"

    if trend_dim == "ranging" and vol_dim == "normal":
        return True, "selective"

    if trend_dim == "ranging" and vol_dim == "high":
        return False, "skip"

    return True, "selective"


def _detect_regime(
    ohlc: List[Dict[str, Any]],
    lookback_fast: int,
    lookback_slow: int,
    lookback_vol: int,
    er_threshold: float,
    atr_ratio_threshold: float,
    impulse_tr_mult: float,
    digestion_bars: int,
    prev_state: Optional[Dict[str, Any]],
) -> Tuple[str, Dict[str, Any]]:
    closes = [float(r["c"]) for r in ohlc if r.get("c") is not None]
    atrs = [float(r["atr"]) for r in ohlc if r.get("atr") is not None]
    if len(closes) < max(lookback_slow + 1, 3) or len(atrs) < lookback_vol:
        return "unknown", {
            "trend_dimension": "unknown",
            "volatility_dimension": "unknown",
            "confidence": 0.0,
            "metrics": {},
            "phase": "normal",
            "trade_allowed": True,
            "risk_mode": "selective",
            "digestion_left": int(prev_state.get("digestion_left") or 0) if prev_state else 0,
        }

    er_fast = _compute_efficiency_ratio(closes, lookback_fast)
    er_slow = _compute_efficiency_ratio(closes, lookback_slow)
    atr_ratio = _compute_atr_ratio(atrs, lookback_vol)

    # Trend dimension: allow fast ER to declare trend early (helps transitions)
    trend_label = "ranging"
    price_change_fast = closes[-1] - closes[-(lookback_fast + 1)] if len(closes) >= lookback_fast + 1 else None
    price_change_slow = closes[-1] - closes[-(lookback_slow + 1)] if len(closes) >= lookback_slow + 1 else None

    er_for_direction = er_fast if (er_fast is not None and er_fast >= er_threshold) else er_slow
    price_change_for_direction = price_change_fast if er_for_direction == er_fast else price_change_slow

    if er_for_direction is not None and er_for_direction >= er_threshold:
        if price_change_for_direction is not None and price_change_for_direction > 0:
            trend_label = "trending_up"
        elif price_change_for_direction is not None and price_change_for_direction < 0:
            trend_label = "trending_down"

    # Vol dimension
    volatility_label = "normal"
    if atr_ratio is not None and atr_ratio >= atr_ratio_threshold:
        volatility_label = "high"

    # Phase: detect impulse via TR spike vs ATR, then hold "digestion" for N bars
    phase = "normal"
    impulse_detected = False

    if len(ohlc) >= 2:
        prev_close = float(ohlc[-2]["c"])
        last = ohlc[-1]
        tr_last = _true_range(
            prev_close=prev_close,
            high=(float(last["h"]) if last.get("h") is not None else None),
            low=(float(last["l"]) if last.get("l") is not None else None),
            close=float(last["c"]),
        )
        atr_now = float(last["atr"]) if last.get("atr") is not None else atrs[-1]
        if atr_now and tr_last >= impulse_tr_mult * atr_now:
            impulse_detected = True
            phase = "impulse"

    digestion_left = int(prev_state.get("digestion_left") or 0) if prev_state else 0
    if impulse_detected:
        digestion_left = digestion_bars
    else:
        if digestion_left > 0:
            digestion_left -= 1
            phase = "digestion"

    # Regime label: keep old behavior, but you also have 2D dims + phase
    if volatility_label == "high":
        regime_label = "high_volatility"
    else:
        regime_label = trend_label

    # Confidence: combine trend + vol (don’t let one dimension always saturate)
    trend_strength = min(1.0, (er_for_direction or 0.0) / er_threshold) if er_threshold else 0.0
    vol_strength = min(1.0, (atr_ratio or 0.0) / atr_ratio_threshold) if atr_ratio_threshold else 0.0
    confidence = round((0.6 * vol_strength + 0.4 * trend_strength), 3)

    trade_allowed, risk_mode = _derive_action(trend_label, volatility_label, phase)

    metrics = {
        "efficiency_ratio_fast": None if er_fast is None else round(er_fast, 4),
        "efficiency_ratio_slow": None if er_slow is None else round(er_slow, 4),
        "atr_ratio": None if atr_ratio is None else round(atr_ratio, 4),
        "trend_lookback_fast": lookback_fast,
        "trend_lookback_slow": lookback_slow,
        "vol_lookback": lookback_vol,
        "er_threshold": er_threshold,
        "atr_ratio_threshold": atr_ratio_threshold,
        "price_change_fast": None if price_change_fast is None else round(price_change_fast, 4),
        "price_change_slow": None if price_change_slow is None else round(price_change_slow, 4),
    }

    return regime_label, {
        "trend_dimension": trend_label,
        "volatility_dimension": volatility_label,
        "confidence": confidence,
        "metrics": metrics,
        "phase": phase,
        "digestion_left": digestion_left,
        "trade_allowed": trade_allowed,
        "risk_mode": risk_mode,
    }


def _apply_hysteresis(
    now: datetime,
    state: Optional[Dict[str, Any]],
    detected_regime: str,
    detected_meta: Dict[str, Any],
    timeframe: str,
    symbol: str,
    confirmations: int,
) -> Dict[str, Any]:
    now_iso = now.astimezone(timezone.utc).isoformat()

    # Backward compat defaults
    if not state or state.get("regime") is None:
        return {
            "regime": detected_regime,
            "current_since": now_iso,
            "pending_regime": None,
            "pending_count": 0,
            "stable_count": 0,
            "consecutive_same": 0,  # alias of pending_count (legacy)
            "last_updated": now_iso,
            "timeframe": timeframe,
            "symbol": symbol,
            **detected_meta,
        }

    current_regime = state.get("regime")
    pending_regime = state.get("pending_regime")
    pending_count = int(state.get("pending_count") or state.get("consecutive_same") or 0)
    stable_count = int(state.get("stable_count") or 0)

    if detected_regime == current_regime:
        pending_regime = None
        pending_count = 0
        stable_count += 1
    else:
        stable_count = 0
        if pending_regime == detected_regime:
            pending_count += 1
        else:
            pending_regime = detected_regime
            pending_count = 1

        if pending_count >= confirmations:
            current_regime = detected_regime
            pending_regime = None
            pending_count = 0
            stable_count = 0
            state["current_since"] = now_iso

    state.update(
        {
            "regime": current_regime,
            "pending_regime": pending_regime,
            "pending_count": pending_count,
            "stable_count": stable_count,
            "consecutive_same": pending_count,  # keep legacy field
            "last_updated": now_iso,
            "timeframe": timeframe,
            "symbol": symbol,
            **detected_meta,
        }
    )
    if state.get("current_since") is None:
        state["current_since"] = now_iso
    return state


def get_market_state(
    timeframe: Optional[str] = None,
    symbol: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    settings = _get_settings()
    timeframe = timeframe or settings.default_timeframe
    table = settings.table_map.get(timeframe, settings.table_map[settings.default_timeframe])
    symbol = _normalize_symbol(symbol)
    now = now or datetime.now(timezone.utc)

    # ✅ PER-TIMEFRAME PERSISTENCE
    path = _format_state_path(settings.state_path, symbol=symbol, timeframe=timeframe)

    state = _load_state(path)
    if state:
        last_updated = state.get("last_updated")
        # We no longer discard on timeframe mismatch because the path is per-timeframe.
        # Still sanity-check symbol/timeframe if present.
        last_timeframe = state.get("timeframe")
        last_symbol = state.get("symbol")
        if last_timeframe and last_timeframe != timeframe:
            state = None
        elif last_symbol and last_symbol != symbol:
            state = None
        elif last_updated:
            try:
                last_dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                age_seconds = (now - last_dt).total_seconds()
                if age_seconds < settings.refresh_seconds:
                    return state
            except Exception:
                pass

    limit = max(settings.trend_lookback + 2, settings.vol_lookback + 2, settings.fast_trend_lookback + 2)
    bars = _fetch_bars(table, symbol, limit)
    if not bars:
        logging.warning("Market regime: no bars found for %s in %s", symbol, table)
        return state or {
            "regime": None,
            "current_since": None,
            "pending_regime": None,
            "pending_count": 0,
            "stable_count": 0,
            "consecutive_same": 0,
            "last_updated": now.astimezone(timezone.utc).isoformat(),
            "timeframe": timeframe,
            "symbol": symbol,
            "trend_dimension": None,
            "volatility_dimension": None,
            "confidence": 0.0,
            "metrics": {},
            "phase": "normal",
            "digestion_left": int(state.get("digestion_left") or 0) if state else 0,
            "trade_allowed": True,
            "risk_mode": "selective",
        }

    bars_sorted = sorted(bars, key=lambda row: row.get("ts") or "")

    # Filter out rows missing c/atr
    ohlc = [r for r in bars_sorted if r.get("c") is not None and r.get("atr") is not None]
    if len(ohlc) < max(settings.trend_lookback + 1, settings.vol_lookback):
        logging.warning("Market regime: insufficient data for %s in %s", symbol, table)
        return state or {
            "regime": None,
            "current_since": None,
            "pending_regime": None,
            "pending_count": 0,
            "stable_count": 0,
            "consecutive_same": 0,
            "last_updated": now.astimezone(timezone.utc).isoformat(),
            "timeframe": timeframe,
            "symbol": symbol,
            "trend_dimension": None,
            "volatility_dimension": None,
            "confidence": 0.0,
            "metrics": {},
            "phase": "normal",
            "digestion_left": int(state.get("digestion_left") or 0) if state else 0,
            "trade_allowed": True,
            "risk_mode": "selective",
        }

    detected_regime, detected_meta = _detect_regime(
        ohlc=ohlc,
        lookback_fast=settings.fast_trend_lookback,
        lookback_slow=settings.trend_lookback,
        lookback_vol=settings.vol_lookback,
        er_threshold=settings.er_threshold,
        atr_ratio_threshold=settings.atr_ratio_threshold,
        impulse_tr_mult=settings.impulse_tr_mult,
        digestion_bars=settings.digestion_bars,
        prev_state=state,
    )

    updated_state = _apply_hysteresis(
        now=now,
        state=state,
        detected_regime=detected_regime,
        detected_meta=detected_meta,
        timeframe=timeframe,
        symbol=symbol,
        confirmations=settings.confirmations,
    )
    _write_state(path, updated_state)
    return updated_state


def get_market_regime_label(timeframe: Optional[str] = None, symbol: Optional[str] = None) -> Optional[str]:
    state = get_market_state(timeframe=timeframe, symbol=symbol)
    return state.get("regime") if state else None
