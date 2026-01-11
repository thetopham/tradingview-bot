"""regime_classifier.py

Deterministic market regime classifier designed to *block* trades in chop/range
and allow trades only in **low-volatility trends**.

This is intentionally lightweight (no numpy/pandas) and uses the existing
Supabase indicator tables (tv_datafeed_5m / tv_datafeed_30m / tv_datafeed_15m).

The core idea:
  - Trend direction must be clear (price and EMAs aligned + VWAP)
  - Trend must be "clean" (efficiency ratio high, EMA21 slope meaningful)
  - Volatility must be low *relative to recent* (ATR percentile in recent window)

If the regime is not favorable, return ok=False and a reason string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import logging
import time
import requests
from dateutil import parser


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _pct_rank_last(values: List[float]) -> Optional[float]:
    """Percentile rank of the last element within the list (0..1).

    Returns None if list is empty.
    """
    if not values:
        return None
    last = values[-1]
    if last is None:
        return None
    n = 0
    le = 0
    for v in values:
        if v is None:
            continue
        n += 1
        if v <= last:
            le += 1
    if n == 0:
        return None
    return le / n


def _efficiency_ratio(closes: List[float], lookback: int) -> Optional[float]:
    """Kaufman efficiency ratio over `lookback` bars (0..1)."""
    if lookback <= 1:
        return None
    if len(closes) < lookback + 1:
        return None
    c0 = closes[-1 - lookback]
    c1 = closes[-1]
    if c0 is None or c1 is None:
        return None
    direction = abs(c1 - c0)
    volatility = 0.0
    for i in range(len(closes) - lookback, len(closes)):
        a = closes[i - 1]
        b = closes[i]
        if a is None or b is None:
            return None
        volatility += abs(b - a)
    if volatility <= 0:
        return 0.0
    return max(0.0, min(1.0, direction / volatility))


def _parse_ts(ts: Any) -> Optional[float]:
    """Parse Supabase `ts` into epoch seconds."""
    try:
        if ts is None:
            return None
        if isinstance(ts, (int, float)):
            return float(ts)
        dt = parser.isoparse(str(ts))
        return dt.timestamp()
    except Exception:
        return None


def _normalize_symbol(sym: str, default: str = "MES") -> str:
    """Normalize contractId-like symbols (e.g., CON.F.US.MES.H26) -> MES."""
    if not sym:
        return default
    s = str(sym).upper()
    if s.startswith("CON."):
        parts = s.split(".")
        # CON.F.US.MES.H26 => underlying at index 3
        if len(parts) >= 4:
            return parts[3]
    # If already looks like MES
    if "." not in s and len(s) <= 6:
        return s
    # fallback: try to find known root in the string
    if "MES" in s:
        return "MES"
    return default


@dataclass
class RegimeResult:
    ok: bool
    regime: str
    direction: Optional[str]
    reason: str
    metrics: Dict[str, Any]


class RegimeClassifier:
    """Classify low-volatility trends using Supabase indicator tables."""

    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        *,
        table_5m: str = "tv_datafeed_5m",
        table_htf: str = "tv_datafeed_30m",
        table_htf_fallback: str = "tv_datafeed_15m",
        lookback_bars: int = 140,
        er_lookback: int = 12,
        atr_pctl_lookback: int = 50,
        er_min: float = 0.35,
        atr_pctl_max: float = 0.25,
        ema_spread_atr_min: float = 0.30,
        slope_atr_min: float = 0.50,
        atr_min_points: Optional[float] = None,
        atr_max_points: Optional[float] = None,
        require_htf_align: bool = True,
        fail_closed: bool = True,
        cache_ttl_s: int = 10,
    ):
        self.supabase_url = (supabase_url or "").rstrip("/")
        self.supabase_key = supabase_key or ""
        self.table_5m = table_5m
        self.table_htf = table_htf
        self.table_htf_fallback = table_htf_fallback

        self.lookback_bars = int(max(60, lookback_bars))
        self.er_lookback = int(max(6, er_lookback))
        self.atr_pctl_lookback = int(max(20, atr_pctl_lookback))

        self.er_min = float(er_min)
        self.atr_pctl_max = float(atr_pctl_max)
        self.ema_spread_atr_min = float(ema_spread_atr_min)
        self.slope_atr_min = float(slope_atr_min)
        self.atr_min_points = float(atr_min_points) if atr_min_points is not None else None
        self.atr_max_points = float(atr_max_points) if atr_max_points is not None else None
        self.require_htf_align = bool(require_htf_align)
        self.fail_closed = bool(fail_closed)

        self.session = requests.Session()
        self.cache_ttl_s = int(max(0, cache_ttl_s))
        self._cache: Dict[Tuple[str, str], Tuple[float, RegimeResult]] = {}

    # ------------------------- Supabase fetch helpers -------------------------

    def _fetch_bars(self, table: str, symbol: str, limit: int) -> List[Dict[str, Any]]:
        if not self.supabase_url or not self.supabase_key:
            return []

        url = f"{self.supabase_url}/rest/v1/{table}"
        params = {
            "symbol": f"eq.{symbol}",
            "select": "ts,o,h,l,c,ema9,ema21,vwap,atr,bb_upper,bb_middle,bb_lower,macd_hist",
            "order": "ts.desc",
            "limit": str(int(limit)),
        }
        headers = {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        resp = self.session.get(url, params=params, headers=headers, timeout=(3.05, 15))
        resp.raise_for_status()
        rows = resp.json() or []
        return rows

    # ------------------------- Core classification logic ----------------------

    def classify(self, sym: str, desired_signal: Optional[str] = None) -> RegimeResult:
        """Return RegimeResult for the *current* market state.

        desired_signal: optional "BUY"/"SELL". If provided, classifier also
        enforces that the detected trend direction matches this intent.
        """

        symbol = _normalize_symbol(sym)
        desired_signal = (desired_signal or "").upper() or None

        cache_key = (symbol, desired_signal or "ANY")
        now = time.time()

        # cache
        if self.cache_ttl_s > 0:
            cached = self._cache.get(cache_key)
            if cached and (now - cached[0] <= self.cache_ttl_s):
                return cached[1]

        # ------------------------------------------------------------------
        # Fetch recent 5m bars
        # ------------------------------------------------------------------
        min_needed = max(self.lookback_bars, self.er_lookback + 5, self.atr_pctl_lookback + 5)
        try:
            bars_5m_desc = self._fetch_bars(self.table_5m, symbol, limit=min_needed)
        except Exception as exc:
            msg = f"regime fetch failed ({self.table_5m}): {exc}"
            logging.error("[Regime] %s", msg)
            res = RegimeResult(
                ok=(not self.fail_closed),
                regime="unknown",
                direction=None,
                reason=msg,
                metrics={"symbol": symbol, "table": self.table_5m},
            )
            self._cache[cache_key] = (now, res)
            return res

        bars_5m = list(reversed(bars_5m_desc))  # oldest -> newest
        if len(bars_5m) < max(self.er_lookback + 1, self.atr_pctl_lookback):
            msg = f"insufficient bars ({len(bars_5m)})"
            res = RegimeResult(
                ok=(not self.fail_closed),
                regime="unknown",
                direction=None,
                reason=msg,
                metrics={"symbol": symbol, "table": self.table_5m, "bars": len(bars_5m)},
            )
            self._cache[cache_key] = (now, res)
            return res

        closes = [_as_float(b.get("c")) for b in bars_5m]
        ema9s = [_as_float(b.get("ema9")) for b in bars_5m]
        ema21s = [_as_float(b.get("ema21")) for b in bars_5m]
        vwaps = [_as_float(b.get("vwap")) for b in bars_5m]
        atrs = [_as_float(b.get("atr")) for b in bars_5m]
        macdh = [_as_float(b.get("macd_hist")) for b in bars_5m]
        bb_u = [_as_float(b.get("bb_upper")) for b in bars_5m]
        bb_m = [_as_float(b.get("bb_middle")) for b in bars_5m]
        bb_l = [_as_float(b.get("bb_lower")) for b in bars_5m]

        c = closes[-1]
        ema9 = ema9s[-1]
        ema21 = ema21s[-1]
        vwap = vwaps[-1]
        atr = atrs[-1]
        macd_hist = macdh[-1]
        ts_epoch = _parse_ts(bars_5m[-1].get("ts"))

        # If critical values missing: fail closed/open depending on config
        critical = (c, ema9, ema21, vwap, atr)
        if any(v is None for v in critical) or (atr is not None and atr <= 0):
            msg = "missing indicator values"
            res = RegimeResult(
                ok=(not self.fail_closed),
                regime="unknown",
                direction=None,
                reason=msg,
                metrics={"symbol": symbol, "table": self.table_5m, "ts": bars_5m[-1].get("ts")},
            )
            self._cache[cache_key] = (now, res)
            return res

        # Trend direction from EMA/VWAP alignment
        direction = None
        if (c > ema9 > ema21) and (c > vwap):
            direction = "UP"
        elif (c < ema9 < ema21) and (c < vwap):
            direction = "DOWN"

        # Must match desired signal if provided
        if desired_signal == "BUY" and direction != "UP":
            res = RegimeResult(
                ok=False,
                regime="blocked",
                direction=direction,
                reason=f"direction mismatch (wanted BUY, got {direction})",
                metrics={"symbol": symbol, "c": c, "ema9": ema9, "ema21": ema21, "vwap": vwap},
            )
            self._cache[cache_key] = (now, res)
            return res
        if desired_signal == "SELL" and direction != "DOWN":
            res = RegimeResult(
                ok=False,
                regime="blocked",
                direction=direction,
                reason=f"direction mismatch (wanted SELL, got {direction})",
                metrics={"symbol": symbol, "c": c, "ema9": ema9, "ema21": ema21, "vwap": vwap},
            )
            self._cache[cache_key] = (now, res)
            return res

        # Trend strength
        er = _efficiency_ratio(closes, self.er_lookback)

        # EMA21 slope over ER window (normalized by ATR)
        ema21_prev = ema21s[-1 - self.er_lookback] if len(ema21s) >= self.er_lookback + 1 else None
        ema21_slope = (ema21 - ema21_prev) if (ema21_prev is not None) else None
        slope_norm = (abs(ema21_slope) / atr) if (ema21_slope is not None and atr) else None

        # EMA spread normalized by ATR
        ema_spread_atr = abs(ema9 - ema21) / atr if atr else None

        # Volatility filter: ATR percentile rank within recent window (lower = calmer)
        atr_window = atrs[-self.atr_pctl_lookback :]
        atr_pctl = _pct_rank_last(atr_window)

        # Optional MACD alignment (cheap extra gate)
        macd_ok = None
        if macd_hist is not None and direction is not None:
            macd_ok = (macd_hist > 0) if direction == "UP" else (macd_hist < 0)

        # Bollinger width (informational)
        bb_width = None
        if bb_u[-1] is not None and bb_l[-1] is not None and bb_m[-1] not in (None, 0):
            bb_width = (bb_u[-1] - bb_l[-1]) / bb_m[-1]

        metrics = {
            "symbol": symbol,
            "ts": bars_5m[-1].get("ts"),
            "ts_epoch": ts_epoch,
            "direction": direction,
            "c": c,
            "ema9": ema9,
            "ema21": ema21,
            "vwap": vwap,
            "atr": atr,
            "atr_min_points": self.atr_min_points,
            "atr_max_points": self.atr_max_points,
            "atr_pctl": atr_pctl,
            "er": er,
            "ema21_slope": ema21_slope,
            "slope_norm": slope_norm,
            "ema_spread_atr": ema_spread_atr,
            "macd_hist": macd_hist,
            "macd_ok": macd_ok,
            "bb_width": bb_width,
            "table_5m": self.table_5m,
        }

        reasons = []

        # Optional absolute ATR gates (instrument-specific)
        if self.atr_min_points is not None and atr < self.atr_min_points:
            reasons.append(f"ATR<{self.atr_min_points:.2f}")
        if self.atr_max_points is not None and atr > self.atr_max_points:
            reasons.append(f"ATR>{self.atr_max_points:.2f}")

        if direction is None:
            reasons.append("no EMA/VWAP alignment")
        if er is None or er < self.er_min:
            reasons.append(f"ER<{self.er_min:.2f}")
        if ema_spread_atr is None or ema_spread_atr < self.ema_spread_atr_min:
            reasons.append(f"EMA_spread/ATR<{self.ema_spread_atr_min:.2f}")
        if slope_norm is None or slope_norm < self.slope_atr_min:
            reasons.append(f"EMA21_slope/ATR<{self.slope_atr_min:.2f}")
        if atr_pctl is None or atr_pctl > self.atr_pctl_max:
            reasons.append(f"ATR_pctl>{self.atr_pctl_max:.2f}")

        # directional slope sign check
        if direction == "UP" and ema21_slope is not None and ema21_slope <= 0:
            reasons.append("EMA21 slope not up")
        if direction == "DOWN" and ema21_slope is not None and ema21_slope >= 0:
            reasons.append("EMA21 slope not down")

        # macd check is soft by default (don't block if missing)
        if macd_ok is False:
            reasons.append("MACD_hist not aligned")

        # ------------------------------------------------------------------
        # Higher timeframe alignment gate (30m preferred; fallback 15m)
        # ------------------------------------------------------------------
        htf_ok = True
        htf_used = None
        htf_reason = None

        if self.require_htf_align and direction is not None:
            for table in (self.table_htf, self.table_htf_fallback):
                if not table:
                    continue
                try:
                    bars_htf_desc = self._fetch_bars(table, symbol, limit=20)
                    bars_htf = list(reversed(bars_htf_desc))
                    if not bars_htf:
                        continue

                    hc = _as_float(bars_htf[-1].get("c"))
                    he9 = _as_float(bars_htf[-1].get("ema9"))
                    he21 = _as_float(bars_htf[-1].get("ema21"))
                    hvwap = _as_float(bars_htf[-1].get("vwap"))

                    if None in (hc, he9, he21, hvwap):
                        continue

                    if direction == "UP":
                        ok = (hc > he9 > he21) and (hc > hvwap)
                    else:
                        ok = (hc < he9 < he21) and (hc < hvwap)

                    htf_used = table
                    if ok:
                        htf_ok = True
                        htf_reason = None
                    else:
                        htf_ok = False
                        htf_reason = "HTF not aligned"
                    break
                except Exception as exc:
                    logging.warning("[Regime] HTF fetch failed (%s): %s", table, exc)
                    continue

            metrics["htf_table"] = htf_used
            metrics["htf_ok"] = htf_ok

            if htf_ok is False:
                reasons.append(htf_reason or "HTF misaligned")

        ok = (len(reasons) == 0)
        if ok:
            regime = "lv_trend_up" if direction == "UP" else "lv_trend_down"
            reason = "ok"
        else:
            regime = "blocked"
            reason = "; ".join(reasons)

        res = RegimeResult(ok=ok, regime=regime, direction=direction, reason=reason, metrics=metrics)
        self._cache[cache_key] = (now, res)
        return res
