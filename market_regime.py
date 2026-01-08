"""market_regime.py

Lightweight market regime detection with persistence.

Goal
----
Provide a small, *stable* market-state object that can be:
  1) persisted to disk across restarts, and
  2) injected into the Flask -> n8n AI request payload.

Regime labels (coarse on purpose)
-------------------------------
- "trending"        : directional market (trend strength above threshold)
- "ranging"         : low directional bias / mean-reversion
- "high_volatility" : volatility expansion (ATR spike) regardless of trend

Inputs
------
This module expects recent OHLC/indicator rows to exist in Supabase tables
populated by your TradingView datafeed workflows (e.g. tv_datafeed_30m).

The detector is intentionally simple and fast:
  - Trend strength = EMA21 slope over N bars, normalized by ATR
  - Volatility     = ATR ratio vs rolling median ATR

It also applies hysteresis ("confirm" count) so regimes don't flip-flop.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except Exception:
        return None


def _normalize_symbol(sym: str) -> str:
    """Convert contract-like symbols into the TradingView root symbol.

    Examples:
      - "CON.F.US.MES.H26" -> "MES"
      - "MES"             -> "MES"
    """
    sym = (sym or "").strip()
    if not sym:
        return sym
    parts = sym.split(".")
    # Common ProjectX format: CON.F.US.<ROOT>.<MONTH>
    if len(parts) >= 4 and parts[0].upper() == "CON":
        return parts[3].upper()
    return sym.upper()


def _infer_timeframe_from_table(table: str) -> str:
    t = (table or "").lower()
    if "_5m" in t:
        return "5m"
    if "_15m" in t:
        return "15m"
    if "_30m" in t:
        return "30m"
    if "_1d" in t or "_d" in t:
        return "1d"
    return "unknown"


def _compute_regime_from_bars(
    bars: List[Dict[str, Any]],
    *,
    slope_bars: int,
    trend_threshold: float,
    range_threshold: float,
    vol_lookback: int,
    vol_ratio_threshold: float,
) -> Dict[str, Any]:
    """Compute a regime snapshot from recent bars.

    Required columns (best effort):
      - ema21
      - atr
      - bb_upper, bb_lower (optional)
      - c (close)
    """

    if not bars:
        return {
            "regime_raw": "unknown",
            "trend": "flat",
            "confidence": 0,
            "slope_norm": None,
            "atr": None,
            "atr_ratio": None,
            "bb_width_atr": None,
            "supporting_factors": ["no_bars"],
        }

    # Ensure bars are oldest->newest for indexing.
    bars = sorted(bars, key=lambda b: str(b.get("ts") or ""))
    last = bars[-1]

    ema_series: List[Optional[float]] = [_safe_float(b.get("ema21")) for b in bars]
    close_series: List[Optional[float]] = [_safe_float(b.get("c")) for b in bars]
    atr_series: List[Optional[float]] = [_safe_float(b.get("atr")) for b in bars]

    # Fallbacks: if ema21 is missing, use close.
    if all(v is None for v in ema_series):
        ema_series = close_series

    ema_last = ema_series[-1]
    atr_last = atr_series[-1]
    if atr_last is None or atr_last <= 0:
        # Try median fallback
        atr_candidates = [v for v in atr_series if v is not None and v > 0]
        atr_last = median(atr_candidates) if atr_candidates else None

    # Trend slope over slope_bars bars.
    slope_norm = None
    trend_dir = "flat"
    if (
        ema_last is not None
        and atr_last is not None
        and atr_last > 0
        and len(ema_series) > slope_bars
        and ema_series[-1 - slope_bars] is not None
    ):
        ema_prev = ema_series[-1 - slope_bars]
        slope_norm = (ema_last - float(ema_prev)) / float(atr_last)
        if slope_norm > 0.05:
            trend_dir = "up"
        elif slope_norm < -0.05:
            trend_dir = "down"
        else:
            trend_dir = "flat"

    # Volatility ratio vs rolling median.
    atr_ratio = None
    atr_med = None
    atr_candidates2 = [v for v in atr_series if v is not None and v > 0]
    if atr_last is not None and atr_last > 0 and atr_candidates2:
        look = atr_candidates2[-int(vol_lookback) :] if vol_lookback else atr_candidates2
        if look:
            atr_med = median(look)
        if atr_med and atr_med > 0:
            atr_ratio = float(atr_last) / float(atr_med)

    # Optional BB width in ATR units.
    bb_upper = _safe_float(last.get("bb_upper"))
    bb_lower = _safe_float(last.get("bb_lower"))
    bb_width_atr = None
    if bb_upper is not None and bb_lower is not None and atr_last is not None and atr_last > 0:
        bb_width_atr = (bb_upper - bb_lower) / float(atr_last)

    # Classify.
    abs_slope = abs(slope_norm) if slope_norm is not None else 0.0
    is_trending = slope_norm is not None and abs_slope >= float(trend_threshold)
    is_ranging = slope_norm is not None and abs_slope <= float(range_threshold)
    is_high_vol = atr_ratio is not None and atr_ratio >= float(vol_ratio_threshold)

    if is_high_vol:
        regime_raw = "high_volatility"
    elif is_trending:
        regime_raw = "trending"
    else:
        regime_raw = "ranging" if is_ranging or slope_norm is not None else "unknown"

    # Confidence: coarse but monotonic.
    confidence = 0
    if regime_raw == "high_volatility" and atr_ratio is not None:
        confidence = int(min(100, max(0, (atr_ratio / float(vol_ratio_threshold)) * 100)))
    elif regime_raw == "trending" and slope_norm is not None:
        confidence = int(min(100, max(0, (abs_slope / float(trend_threshold)) * 100)))
    elif regime_raw == "ranging" and slope_norm is not None:
        # 100 when slope is ~0, 0 when it reaches range_threshold
        confidence = int(min(100, max(0, (1.0 - (abs_slope / max(float(range_threshold), 1e-9))) * 100)))

    supporting = []
    if slope_norm is not None:
        supporting.append(f"EMA21 slope over {slope_bars} bars")
    if atr_last is not None:
        supporting.append(f"ATR14={float(atr_last):.4f}")
    if atr_ratio is not None:
        supporting.append(f"ATR ratio={float(atr_ratio):.2f}x")
    if bb_width_atr is not None:
        supporting.append(f"BB width={float(bb_width_atr):.2f} ATR")

    return {
        "regime_raw": regime_raw,
        "trend": trend_dir,
        "confidence": confidence,
        "slope_norm": slope_norm,
        "atr": atr_last,
        "atr_median": atr_med,
        "atr_ratio": atr_ratio,
        "bb_width_atr": bb_width_atr,
        "supporting_factors": supporting,
    }


class MarketRegimeService:
    """Fetches recent bars, computes regime, and persists a stable state to disk."""

    def __init__(self):
        # Persistence
        self.state_path = Path(os.environ.get("MARKET_STATE_PATH", "./market_state.json"))
        self.state_bak_path = self.state_path.with_suffix(self.state_path.suffix + ".bak")

        # Data sources
        default_tables = "tv_datafeed_30m,tv_datafeed_15m,tv_datafeed_5m"
        self.tables = [t.strip() for t in os.environ.get("MARKET_STATE_TABLES", default_tables).split(",") if t.strip()]
        self.lookback = int(os.environ.get("MARKET_STATE_LOOKBACK", "200"))

        # Regime params
        self.slope_bars = int(os.environ.get("MARKET_STATE_SLOPE_BARS", "20"))
        self.trend_threshold = float(os.environ.get("MARKET_STATE_TREND_THRESHOLD", "0.5"))
        self.range_threshold = float(os.environ.get("MARKET_STATE_RANGE_THRESHOLD", "0.2"))
        self.vol_lookback = int(os.environ.get("MARKET_STATE_VOL_LOOKBACK", "50"))
        self.vol_ratio_threshold = float(os.environ.get("MARKET_STATE_VOL_RATIO_THRESHOLD", "1.5"))
        self.confirm_updates = int(os.environ.get("MARKET_STATE_CONFIRM_UPDATES", "2"))

        # Caching
        self.cache_seconds = float(os.environ.get("MARKET_STATE_CACHE_SECONDS", "25"))
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

        self._lock = threading.RLock()
        self._state = self._load_state()

    # ----------------------------- Persistence ----------------------------

    def _load_state(self) -> Dict[str, Any]:
        with self._lock:
            data = None
            primary_err = None

            def _load(path: Path):
                return json.loads(path.read_text(encoding="utf-8"))

            try:
                if self.state_path.exists():
                    data = _load(self.state_path)
            except Exception as exc:
                primary_err = exc
                logger.warning("Failed to parse market state from %s: %s", self.state_path, exc)

            if data is None and self.state_bak_path.exists():
                try:
                    data = _load(self.state_bak_path)
                    logger.warning("Recovered market state from backup %s", self.state_bak_path)
                except Exception as exc:
                    logger.warning("Failed to parse market state backup %s: %s", self.state_bak_path, exc)

            if data is None:
                if primary_err:
                    logger.warning("Starting with empty market state due to parse errors: %s", primary_err)
                return {"schema_version": 1, "saved_at": None, "symbols": {}}

            if not isinstance(data, dict):
                return {"schema_version": 1, "saved_at": None, "symbols": {}}

            data.setdefault("schema_version", 1)
            data.setdefault("symbols", {})
            return data

    def _save_state(self) -> None:
        with self._lock:
            self._state["saved_at"] = _utc_now_iso()
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")

            try:
                if self.state_path.exists():
                    try:
                        self.state_bak_path.write_bytes(self.state_path.read_bytes())
                    except Exception as exc:
                        logger.warning("Failed to write market state backup: %s", exc)

                tmp.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.state_path)
            except Exception as exc:
                logger.error("Failed to persist market state to %s: %s", self.state_path, exc)

    # ----------------------------- Supabase -------------------------------

    def _fetch_bars(self, table: str, symbol: str, limit: int) -> List[Dict[str, Any]]:
        """Fetch recent bars from Supabase.

        Uses the supabase python client that is already used elsewhere.
        """
        try:
            from api import get_supabase_client

            sb = get_supabase_client()
            # Select only what we need for regime detection.
            cols = "ts,c,ema21,atr,bb_upper,bb_lower,symbol,timeframe"
            q = (
                sb.table(table)
                .select(cols)
                .eq("symbol", symbol)
                .order("ts", desc=True)
                .limit(int(limit))
            )
            resp = q.execute()
            rows = resp.data or []
            return rows
        except Exception as exc:
            logger.debug("Supabase fetch failed table=%s symbol=%s: %s", table, symbol, exc)
            return []

    # --------------------------- Public API ------------------------------

    def get_market_state(self, symbol: str) -> Dict[str, Any]:
        """Return the *stable* market state for the given symbol."""
        root = _normalize_symbol(symbol)
        if not root:
            return {
                "timestamp": _utc_now_iso(),
                "symbol": symbol,
                "regime": "unknown",
                "trend": "flat",
                "confidence": 0,
            }

        now_ts = time.time()
        cached = self._cache.get(root)
        if cached and now_ts - cached[0] < self.cache_seconds:
            return cached[1]

        with self._lock:
            # Re-check cache inside lock.
            cached = self._cache.get(root)
            if cached and now_ts - cached[0] < self.cache_seconds:
                return cached[1]

            source_table = None
            bars: List[Dict[str, Any]] = []
            for table in self.tables:
                rows = self._fetch_bars(table, root, self.lookback)
                # Need enough bars for slope computation.
                if len(rows) >= (self.slope_bars + 2):
                    source_table = table
                    bars = rows
                    break

            snap = _compute_regime_from_bars(
                bars,
                slope_bars=self.slope_bars,
                trend_threshold=self.trend_threshold,
                range_threshold=self.range_threshold,
                vol_lookback=self.vol_lookback,
                vol_ratio_threshold=self.vol_ratio_threshold,
            )

            prev = (self._state.get("symbols") or {}).get(root) or {}
            prev_regime = prev.get("regime")
            prev_streak = int(prev.get("streak") or 0)

            raw_regime = snap.get("regime_raw") or "unknown"
            stable_regime = prev_regime or raw_regime

            pending_regime = prev.get("pending_regime")
            pending_count = int(prev.get("pending_count") or 0)

            if prev_regime is None:
                stable_regime = raw_regime
                streak = 1
                pending_regime = None
                pending_count = 0
            elif raw_regime == prev_regime:
                stable_regime = prev_regime
                streak = prev_streak + 1
                pending_regime = None
                pending_count = 0
            else:
                # Potential switch; require confirmation to avoid flip-flop.
                if pending_regime != raw_regime:
                    pending_regime = raw_regime
                    pending_count = 1
                else:
                    pending_count += 1

                if pending_count >= max(1, self.confirm_updates):
                    stable_regime = raw_regime
                    streak = 1
                    pending_regime = None
                    pending_count = 0
                else:
                    stable_regime = prev_regime
                    streak = prev_streak + 1

            timeframe = _infer_timeframe_from_table(source_table or "")
            state_row = {
                "updated_at": _utc_now_iso(),
                "symbol": root,
                "timeframe": timeframe,
                "table": source_table,
                "regime": stable_regime,
                "trend": snap.get("trend") or "flat",
                "confidence": snap.get("confidence") or 0,
                "slope_norm": snap.get("slope_norm"),
                "atr": snap.get("atr"),
                "atr_median": snap.get("atr_median"),
                "atr_ratio": snap.get("atr_ratio"),
                "bb_width_atr": snap.get("bb_width_atr"),
                "supporting_factors": snap.get("supporting_factors") or [],
                "streak": int(streak),
                "pending_regime": pending_regime,
                "pending_count": pending_count,
                "params": {
                    "slope_bars": self.slope_bars,
                    "trend_threshold": self.trend_threshold,
                    "range_threshold": self.range_threshold,
                    "vol_lookback": self.vol_lookback,
                    "vol_ratio_threshold": self.vol_ratio_threshold,
                    "confirm_updates": self.confirm_updates,
                },
                "source": {
                    "bars": len(bars),
                },
            }

            # Persist.
            self._state.setdefault("symbols", {})[root] = state_row
            self._save_state()

            public_state = {
                "timestamp": state_row["updated_at"],
                "symbol": root,
                "timeframe": timeframe,
                "regime": state_row["regime"],
                "trend": state_row["trend"],
                "confidence": state_row["confidence"],
                "slope_norm": state_row["slope_norm"],
                "atr": state_row["atr"],
                "atr_ratio": state_row["atr_ratio"],
                "bb_width_atr": state_row["bb_width_atr"],
                "streak": state_row["streak"],
                "supporting_factors": state_row["supporting_factors"],
                "source": {
                    "table": source_table,
                    "bars": len(bars),
                },
            }

            self._cache[root] = (now_ts, public_state)
            return public_state

    def read_persisted_state(self) -> Dict[str, Any]:
        """Return the full persisted state (all symbols)."""
        with self._lock:
            return json.loads(json.dumps(self._state))
