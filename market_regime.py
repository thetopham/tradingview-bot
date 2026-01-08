import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, List, Optional

import requests
from dateutil import parser

from config import load_config


config = load_config()
SUPABASE_URL = config.get("SUPABASE_URL")
SUPABASE_KEY = config.get("SUPABASE_KEY")
MT = config.get("MT")

DEFAULT_SYMBOL = os.environ.get("MARKET_REGIME_SYMBOL", "MES")
DEFAULT_TIMEFRAME = os.environ.get("MARKET_REGIME_TIMEFRAME", "1m")
DEFAULT_LOOKBACK = int(os.environ.get("MARKET_REGIME_LOOKBACK", "60"))
DEFAULT_REFRESH_SECONDS = int(os.environ.get("MARKET_REGIME_REFRESH_SECONDS", "60"))
DEFAULT_VOL_THRESHOLD = float(os.environ.get("MARKET_REGIME_VOL_THRESHOLD", "0.0025"))
DEFAULT_TREND_THRESHOLD = float(os.environ.get("MARKET_REGIME_TREND_THRESHOLD", "0.55"))

STATE_PATH = Path(os.environ.get("MARKET_STATE_PATH", "./market_state.json"))
STATE_BAK_PATH = STATE_PATH.with_suffix(STATE_PATH.suffix + ".bak")

_state_lock = threading.RLock()
_session = requests.Session()


def _now_iso():
    return datetime.now(MT).isoformat()


def _normalize_symbol(symbol: Optional[str]) -> str:
    if not symbol:
        return DEFAULT_SYMBOL
    symbol = symbol.strip().upper()
    if symbol.startswith("CON.") or "." in symbol:
        return DEFAULT_SYMBOL
    return symbol


def _load_state() -> Optional[Dict]:
    with _state_lock:
        primary_error = None
        data = None

        def _load(path: Path) -> Dict:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)

        try:
            if STATE_PATH.exists():
                data = _load(STATE_PATH)
        except Exception as exc:
            primary_error = exc
            logging.warning("Failed to parse market state from %s: %s", STATE_PATH, exc)

        if data is None and STATE_BAK_PATH.exists():
            try:
                data = _load(STATE_BAK_PATH)
                logging.warning("Recovered market state from backup %s after parse failure", STATE_BAK_PATH)
            except Exception as exc:
                logging.warning("Failed to parse backup market state from %s: %s", STATE_BAK_PATH, exc)

        if data is None and primary_error:
            logging.warning("Starting with empty market state due to parse errors: %s", primary_error)

        return data


def _save_state(state: Dict) -> None:
    with _state_lock:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")

        try:
            if STATE_PATH.exists():
                try:
                    STATE_BAK_PATH.write_bytes(STATE_PATH.read_bytes())
                except Exception as exc:
                    logging.warning("Failed to write backup market state to %s: %s", STATE_BAK_PATH, exc)

            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp_path, STATE_PATH)
        except Exception as exc:
            logging.error("Failed to save market state to %s: %s", STATE_PATH, exc)


def _timeframe_variants(timeframe: str) -> List[str]:
    variants = {str(timeframe)}
    if str(timeframe).endswith("m"):
        variants.add(str(timeframe)[:-1])
    else:
        variants.add(f"{timeframe}m")
    return sorted(variants)


def _fetch_recent_bars(symbol: str, timeframe: str, limit: int) -> List[Dict]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        logging.debug("Supabase credentials missing; cannot fetch regime bars")
        return []

    encoded_timeframes = ",".join(f"\"{tf}\"" for tf in _timeframe_variants(timeframe))
    params = {
        "symbol": f"eq.{symbol}",
        "timeframe": f"in.({encoded_timeframes})",
        "select": "o,h,l,c,ts",
        "order": "ts.desc",
        "limit": limit,
    }
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        resp = _session.get(f"{SUPABASE_URL}/rest/v1/tv_datafeed", params=params, headers=headers, timeout=(3.05, 10))
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return []
        rows.reverse()
        bars = []
        for row in rows:
            try:
                bars.append(
                    {
                        "o": float(row.get("o")),
                        "h": float(row.get("h")),
                        "l": float(row.get("l")),
                        "c": float(row.get("c")),
                        "ts": row.get("ts"),
                    }
                )
            except (TypeError, ValueError):
                continue
        return bars
    except Exception as exc:
        logging.warning("Failed to fetch regime bars from Supabase: %s", exc)
        return []


def _compute_regime(bars: List[Dict], vol_threshold: float, trend_threshold: float) -> Dict:
    if len(bars) < 20:
        return {
            "state": "unknown",
            "reason": "insufficient_bars",
            "metrics": {
                "bar_count": len(bars),
            },
        }

    highs = [bar["h"] for bar in bars]
    lows = [bar["l"] for bar in bars]
    closes = [bar["c"] for bar in bars]

    price_range = max(highs) - min(lows)
    close_first = closes[0]
    close_last = closes[-1]
    trend_strength = abs(close_last - close_first) / price_range if price_range else 0.0

    true_ranges = []
    prev_close = None
    for bar in bars:
        high = bar["h"]
        low = bar["l"]
        if prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
        prev_close = bar["c"]

    atr = mean(true_ranges) if true_ranges else 0.0
    volatility_score = (atr / close_last) if close_last else 0.0

    returns = []
    for prev, cur in zip(closes, closes[1:]):
        if prev:
            returns.append((cur - prev) / prev)
    realized_vol = pstdev(returns) if len(returns) >= 2 else 0.0
    vol_metric = max(volatility_score, realized_vol)

    if vol_metric >= vol_threshold:
        state = "high_volatility"
    elif trend_strength >= trend_threshold:
        state = "trending"
    else:
        state = "ranging"

    return {
        "state": state,
        "reason": "computed",
        "metrics": {
            "bar_count": len(bars),
            "close_first": close_first,
            "close_last": close_last,
            "price_range": price_range,
            "atr": atr,
            "volatility_score": volatility_score,
            "realized_volatility": realized_vol,
            "trend_strength": trend_strength,
        },
    }


def get_market_regime_state(
    symbol: Optional[str] = None,
    timeframe: Optional[str] = None,
    lookback: Optional[int] = None,
    refresh_seconds: Optional[int] = None,
) -> Dict:
    symbol = _normalize_symbol(symbol)
    timeframe = timeframe or DEFAULT_TIMEFRAME
    lookback = int(lookback or DEFAULT_LOOKBACK)
    refresh_seconds = int(refresh_seconds or DEFAULT_REFRESH_SECONDS)

    cached = _load_state()
    if cached:
        cached_symbol = cached.get("symbol")
        cached_timeframe = cached.get("timeframe")
        cached_updated = cached.get("updated_at")
        if cached_symbol == symbol and cached_timeframe == timeframe and cached_updated:
            try:
                updated_at = parser.isoparse(cached_updated)
                if time.time() - updated_at.timestamp() <= refresh_seconds:
                    return cached
            except Exception:
                pass

    bars = _fetch_recent_bars(symbol, timeframe, lookback)
    regime = _compute_regime(bars, DEFAULT_VOL_THRESHOLD, DEFAULT_TREND_THRESHOLD)
    payload = {
        "state": regime["state"],
        "reason": regime["reason"],
        "metrics": regime["metrics"],
        "updated_at": _now_iso(),
        "symbol": symbol,
        "timeframe": timeframe,
        "lookback": lookback,
    }
    _save_state(payload)
    return payload
