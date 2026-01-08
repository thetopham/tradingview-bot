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
    state_path: str
    refresh_seconds: int
    trend_lookback: int
    vol_lookback: int
    confirmations: int
    default_timeframe: str
    table_map: Dict[str, str]
    er_threshold: float
    atr_ratio_threshold: float


_SUPABASE_CLIENT = None


def _get_settings() -> RegimeSettings:
    state_path = os.getenv("MARKET_STATE_PATH", "./market_state.json")
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


def _load_state(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        logging.warning("Failed to read market regime state: %s", exc)
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
    supabase = _get_supabase_client()
    result = (
        supabase
        .table(table)
        .select("ts,c,atr")
        .eq("symbol", symbol)
        .order("ts", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def _compute_efficiency_ratio(closes: List[float], lookback: int) -> Optional[float]:
    if len(closes) < lookback + 1:
        return None
    recent = closes[-(lookback + 1):]
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


def _detect_regime(
    closes: List[float],
    atrs: List[float],
    lookback_trend: int,
    lookback_vol: int,
    er_threshold: float,
    atr_ratio_threshold: float,
) -> Tuple[str, Dict[str, Any]]:
    er = _compute_efficiency_ratio(closes, lookback_trend)
    atr_ratio = _compute_atr_ratio(atrs, lookback_vol)

    trend_label = "ranging"
    price_change = None
    if len(closes) >= lookback_trend + 1:
        price_change = closes[-1] - closes[-(lookback_trend + 1)]
    if er is not None and er >= er_threshold:
        if price_change is not None and price_change > 0:
            trend_label = "trending_up"
        elif price_change is not None and price_change < 0:
            trend_label = "trending_down"

    volatility_label = "normal"
    if atr_ratio is not None and atr_ratio >= atr_ratio_threshold:
        volatility_label = "high"

    if volatility_label == "high":
        regime_label = "high_volatility"
    else:
        regime_label = trend_label

    trend_strength = min(1.0, (er or 0.0) / er_threshold) if er_threshold else 0.0
    vol_strength = min(1.0, (atr_ratio or 0.0) / atr_ratio_threshold) if atr_ratio_threshold else 0.0

    if regime_label == "high_volatility":
        confidence = round(vol_strength, 3)
    elif trend_label == "ranging":
        confidence = round(1.0 - trend_strength, 3)
    else:
        confidence = round(trend_strength, 3)

    metrics = {
        "efficiency_ratio": None if er is None else round(er, 4),
        "atr_ratio": None if atr_ratio is None else round(atr_ratio, 4),
        "trend_lookback": lookback_trend,
        "vol_lookback": lookback_vol,
        "er_threshold": er_threshold,
        "atr_ratio_threshold": atr_ratio_threshold,
        "price_change": None if price_change is None else round(price_change, 4),
    }

    return regime_label, {
        "trend_dimension": trend_label,
        "volatility_dimension": volatility_label,
        "confidence": confidence,
        "metrics": metrics,
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
    if not state or state.get("regime") is None:
        return {
            "regime": detected_regime,
            "current_since": now_iso,
            "pending_regime": None,
            "consecutive_same": 0,
            "last_updated": now_iso,
            "timeframe": timeframe,
            "symbol": symbol,
            **detected_meta,
        }

    current_regime = state.get("regime")
    pending_regime = state.get("pending_regime")
    consecutive_same = int(state.get("consecutive_same") or 0)

    if detected_regime == current_regime:
        pending_regime = None
        consecutive_same = 0
    else:
        if pending_regime == detected_regime:
            consecutive_same += 1
        else:
            pending_regime = detected_regime
            consecutive_same = 1

        if consecutive_same >= confirmations:
            current_regime = detected_regime
            pending_regime = None
            consecutive_same = 0
            state["current_since"] = now_iso

    state.update(
        {
            "regime": current_regime,
            "pending_regime": pending_regime,
            "consecutive_same": consecutive_same,
            "last_updated": now_iso,
            "timeframe": timeframe,
            "symbol": symbol,
            **detected_meta,
        }
    )
    return state


def get_market_state(
    timeframe: Optional[str] = None,
    symbol: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    settings = _get_settings()
    timeframe = timeframe or settings.default_timeframe
    table = settings.table_map.get(timeframe)
    if not table:
        fallback_key = (
            settings.default_timeframe
            if settings.default_timeframe in settings.table_map
            else next(iter(settings.table_map), None)
        )
        if fallback_key:
            logging.warning(
                "Market regime: unsupported timeframe %s; falling back to %s",
                timeframe,
                fallback_key,
            )
            table = settings.table_map[fallback_key]
    symbol = _normalize_symbol(symbol)
    now = now or datetime.now(timezone.utc)

    state = _load_state(settings.state_path)
    if state:
        last_updated = state.get("last_updated")
        last_timeframe = state.get("timeframe")
        last_symbol = state.get("symbol")
        if last_timeframe != timeframe or last_symbol != symbol:
            state = None
        elif last_updated:
            try:
                last_dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                age_seconds = (now - last_dt).total_seconds()
                if age_seconds < settings.refresh_seconds:
                    return state
            except Exception:
                pass

    limit = max(settings.trend_lookback + 1, settings.vol_lookback)
    bars = _fetch_bars(table, symbol, limit)
    if not bars:
        logging.warning("Market regime: no bars found for %s in %s", symbol, table)
        return state or {
            "regime": None,
            "current_since": None,
            "pending_regime": None,
            "consecutive_same": 0,
            "last_updated": now.astimezone(timezone.utc).isoformat(),
            "timeframe": timeframe,
            "symbol": symbol,
            "trend_dimension": None,
            "volatility_dimension": None,
            "confidence": 0.0,
            "metrics": {},
        }

    bars_sorted = sorted(bars, key=lambda row: row.get("ts") or "")
    closes = [float(row["c"]) for row in bars_sorted if row.get("c") is not None]
    atrs = [float(row["atr"]) for row in bars_sorted if row.get("atr") is not None]

    if not closes or not atrs:
        logging.warning("Market regime: insufficient data for %s in %s", symbol, table)
        return state or {
            "regime": None,
            "current_since": None,
            "pending_regime": None,
            "consecutive_same": 0,
            "last_updated": now.astimezone(timezone.utc).isoformat(),
            "timeframe": timeframe,
            "symbol": symbol,
            "trend_dimension": None,
            "volatility_dimension": None,
            "confidence": 0.0,
            "metrics": {},
        }

    detected_regime, detected_meta = _detect_regime(
        closes=closes,
        atrs=atrs,
        lookback_trend=settings.trend_lookback,
        lookback_vol=settings.vol_lookback,
        er_threshold=settings.er_threshold,
        atr_ratio_threshold=settings.atr_ratio_threshold,
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
    _write_state(settings.state_path, updated_state)
    return updated_state


def get_market_regime_label(timeframe: Optional[str] = None, symbol: Optional[str] = None) -> Optional[str]:
    state = get_market_state(timeframe=timeframe, symbol=symbol)
    return state.get("regime") if state else None
