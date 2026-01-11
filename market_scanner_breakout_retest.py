#!/usr/bin/env python3
'''
"""
market_scanner_breakout_retest.py

Breakout + retest + resumption scanner (LONG setup).

What it does
------------
1) Continuously pulls recent bars (default: last 200) from:
   - Supabase table (recommended): tv_datafeed_30m / tv_datafeed_15m / tv_datafeed_5m
   - or a local CSV for backtesting / development.

2) Runs a simple *state machine* over incoming bars:
      CONSOLIDATION -> BREAKOUT -> RETEST -> RESUMPTION -> SIGNAL

3) When RESUMPTION confirms, it can:
   - Trigger your local Flask bot (/webhook) so it flows into your existing n8n AI pipeline, OR
   - POST directly to an n8n webhook endpoint.

4) Logs market-state transitions to disk as JSONL and persists the current state to JSON
   so restarts don't "forget" where they were.

Env vars (minimal)
------------------
# If using Supabase:
SUPABASE_URL=...
SUPABASE_KEY=...

# If you want to trigger the local Flask bot (/webhook):
TV_PORT=5000
WEBHOOK_SECRET=...
SCANNER_ACCOUNT=alpha          # or any configured bot account slug
SCANNER_SYMBOL=MES
SCANNER_SIZE=1

# If you want to trigger n8n directly (optional):
N8N_SCANNER_URL=https://...    # webhook URL

Optional tuning env vars
------------------------
SCANNER_TABLE=tv_datafeed_30m
SCANNER_LOOKBACK=200
SCANNER_POLL_SECONDS=20
SCANNER_CONS_LEN=12
SCANNER_CONS_RANGE_ATR_MULT=2.0
SCANNER_BREAKOUT_BUFFER_ATR=0.10
SCANNER_RETEST_WINDOW=8
SCANNER_RESUME_WINDOW=8

Usage
-----
Run once on a CSV (prints detected pattern / levels):
    python market_scanner_breakout_retest.py --csv /path/to/tv_datafeed_30m_rows.csv --once

Run live (Supabase polling):
    python market_scanner_breakout_retest.py

Notes
-----
- This is a *setup scanner*, not an execution engine. Your existing bot can still decide
  position sizing / execution / brackets via AI.
- Thresholds are ATR-normalized and meant to be tuned per symbol/timeframe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    import pandas as pd
except Exception as exc:  # pragma: no cover
    raise RuntimeError("pandas is required for this scanner") from exc


# -----------------------------
# Config / State
# -----------------------------

@dataclass
class ScannerParams:
    cons_len: int = 12
    cons_range_atr_mult: float = 2.0
    breakout_buffer_atr: float = 0.10
    breakout_window: int = 8  # max bars to wait for breakout after consolidation
    retest_window: int = 8
    retest_wick_atr: float = 0.20
    retest_close_atr: float = 0.10
    resume_window: int = 8
    resume_buffer_atr: float = 0.05

    # Optional "quality" filters (set to None/0 to disable)
    min_resistance_touches: int = 2
    resistance_touch_atr: float = 0.25  # bar high within this ATR distance of resistance counts as a touch
    require_rsi_above: float = 50.0
    require_macd_hist_above: Optional[float] = None
    require_close_above_ema21: bool = True

    # Resets / invalidation
    invalidate_on_close_below_support_atr: float = 0.25
    cooldown_bars_after_signal: int = 6


@dataclass
class PatternState:
    stage: str = "WAIT_CONSOLIDATION"
    last_processed_ts: Optional[str] = None

    # Consolidation box
    cons_start_ts: Optional[str] = None
    cons_end_ts: Optional[str] = None
    resistance: Optional[float] = None
    support: Optional[float] = None
    atr_ref: Optional[float] = None

    # Breakout
    breakout_ts: Optional[str] = None
    breakout_high: Optional[float] = None

    # Retest
    retest_ts: Optional[str] = None
    retest_low: Optional[float] = None
    swing_high: Optional[float] = None  # highest high from breakout->retest

    # Signal / dedupe
    last_signal_ts: Optional[str] = None
    cooldown_remaining: int = 0


# -----------------------------
# IO helpers
# -----------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(path: Path) -> PatternState:
    if not path.exists():
        return PatternState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PatternState(**data)
    except Exception:
        # corrupt state -> reset
        return PatternState()


def save_state(path: Path, state: PatternState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# -----------------------------
# Data fetchers
# -----------------------------

def load_bars_from_csv(csv_path: str, *, limit: int = 200) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    if "ts" not in df.columns:
        raise ValueError("CSV must contain a 'ts' column")
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"]).sort_values("ts")
    return df.tail(int(limit)).reset_index(drop=True)


def fetch_bars_from_supabase_rest(
    *,
    table: str,
    symbol: str,
    limit: int = 200,
    timeframe: Optional[str] = None,
) -> pd.DataFrame:
    """
    Pull last `limit` rows from Supabase REST for `table`.

    This avoids importing your bot's config.py (which requires ACCOUNT_* env vars).
    """
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY not configured")

    url = f"{supabase_url}/rest/v1/{table}"

    # NOTE: column names in your feed are o/h/l/c/v/atr/ema21/rsi/macd_hist and ts
    params = {
        "select": "ts,o,h,l,c,v,atr,ema21,rsi,macd_hist,symbol,timeframe",
        "symbol": f"eq.{symbol}",
        "order": "ts.desc",
        "limit": str(int(limit)),
    }
    if timeframe is not None:
        params["timeframe"] = f"eq.{timeframe}"

    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept": "application/json",
    }

    resp = requests.get(url, params=params, headers=headers, timeout=(3.05, 10))
    resp.raise_for_status()
    rows = resp.json() or []
    if not rows:
        return pd.DataFrame(columns=["ts", "o", "h", "l", "c", "v", "atr", "ema21", "rsi", "macd_hist"])
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"]).sort_values("ts")
    for col in ("o", "h", "l", "c", "v", "atr", "ema21", "rsi", "macd_hist"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["o", "h", "l", "c"])
    return df.reset_index(drop=True)


# -----------------------------
# Pattern logic (state machine)
# -----------------------------

def _touch_count(cons: pd.DataFrame, resistance: float, atr: float, touch_atr: float) -> int:
    if atr <= 0:
        return 0
    eps = touch_atr * atr
    return int((cons["h"] >= (resistance - eps)).sum())


class BreakoutRetestResumptionScanner:
    def __init__(
        self,
        *,
        params: ScannerParams,
        state_path: Path,
        events_path: Path,
    ):
        self.params = params
        self.state_path = state_path
        self.events_path = events_path
        self.state = load_state(state_path)

    def _log(self, event: str, **kwargs) -> None:
        record = {
            "ts": _utc_now_iso(),
            "event": event,
            "stage": self.state.stage,
            **kwargs,
        }
        append_jsonl(self.events_path, record)

    def _transition(self, new_stage: str, reason: str, **kwargs) -> None:
        old = self.state.stage
        self.state.stage = new_stage
        self._log("stage_transition", old_stage=old, new_stage=new_stage, reason=reason, **kwargs)

    def _reset(self, reason: str, **kwargs) -> None:
        self._transition("WAIT_CONSOLIDATION", reason, **kwargs)
        # keep last_processed_ts / last_signal_ts, but clear setup
        lp = self.state.last_processed_ts
        ls = self.state.last_signal_ts
        cooldown = self.state.cooldown_remaining
        self.state = PatternState(last_processed_ts=lp, last_signal_ts=ls, cooldown_remaining=cooldown)

    def _cooldown_tick(self) -> None:
        if self.state.cooldown_remaining and self.state.cooldown_remaining > 0:
            self.state.cooldown_remaining -= 1

    def process_bars(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Feed the scanner a *sorted ascending* DataFrame of bars.
        Returns any newly emitted signals (usually 0 or 1).
        """
        if df is None or df.empty:
            return []

        df = df.copy()
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
        df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)

        # Only process bars newer than our last_processed_ts.
        last_ts = pd.to_datetime(self.state.last_processed_ts, utc=True, errors="coerce") if self.state.last_processed_ts else None

        new_signals: List[Dict[str, Any]] = []
        for i in range(len(df)):
            bar = df.iloc[i]
            ts = bar["ts"]
            if last_ts is not None and ts <= last_ts:
                continue

            # Cooldown counts down per new bar
            self._cooldown_tick()

            # Process this bar at index i (with access to history up to i)
            signal = self._step(df.iloc[: i + 1], i)
            if signal:
                new_signals.append(signal)

            # update last processed after step
            self.state.last_processed_ts = ts.isoformat()

        save_state(self.state_path, self.state)
        return new_signals

    def _step(self, hist: pd.DataFrame, i: int) -> Optional[Dict[str, Any]]:
        p = self.params
        bar = hist.iloc[i]

        # If in cooldown, we still keep stage machine, but block signal emission.
        in_cooldown = self.state.cooldown_remaining and self.state.cooldown_remaining > 0

        # Helper: invalidate setup if we lose support too hard
        if self.state.support is not None and self.state.atr_ref:
            if float(bar.get("c", float("nan"))) < (self.state.support - p.invalidate_on_close_below_support_atr * self.state.atr_ref):
                self._reset("close_below_support_invalidation", close=float(bar["c"]), support=self.state.support, atr_ref=self.state.atr_ref)
                return None

        # --------------------------
        # Stage 0: find consolidation
        # --------------------------
        if self.state.stage == "WAIT_CONSOLIDATION":
            if len(hist) < p.cons_len + 1:
                return None

            # Define consolidation as the *previous* cons_len bars (ending at i-1)
            cons = hist.iloc[-(p.cons_len + 1) : -1]
            atr_ref = float(cons["atr"].median()) if "atr" in cons.columns and cons["atr"].notna().any() else float("nan")
            if not (atr_ref and atr_ref > 0):
                return None

            rng = float(cons["h"].max() - cons["l"].min())
            if rng > p.cons_range_atr_mult * atr_ref:
                return None

            resistance = float(cons["h"].max())
            support = float(cons["l"].min())

            touches = _touch_count(cons, resistance, atr_ref, p.resistance_touch_atr)
            if touches < p.min_resistance_touches:
                return None

            self.state.cons_start_ts = cons["ts"].iloc[0].isoformat()
            self.state.cons_end_ts = cons["ts"].iloc[-1].isoformat()
            self.state.resistance = resistance
            self.state.support = support
            self.state.atr_ref = atr_ref

            self._transition(
                "WAIT_BREAKOUT",
                "consolidation_detected",
                cons_range=rng,
                atr_ref=atr_ref,
                resistance=resistance,
                support=support,
                resistance_touches=touches,
            )
            return None

        # ----------------------
        # Stage 1: breakout bar
        # ----------------------
        if self.state.stage == "WAIT_BREAKOUT":
            if self.state.resistance is None or not self.state.atr_ref:
                self._reset("missing_setup_fields")
                return None

            # While waiting for a breakout, refresh the consolidation box using the most recent
            # `cons_len` bars (ending at the prior bar). This prevents the scanner from getting
            # stuck on an older/stale resistance level.
            if len(hist) >= p.cons_len + 1:
                cons = hist.iloc[-(p.cons_len + 1) : -1]
                atr_ref_new = float(cons["atr"].median()) if "atr" in cons.columns and cons["atr"].notna().any() else float("nan")
                if atr_ref_new and atr_ref_new > 0:
                    rng_new = float(cons["h"].max() - cons["l"].min())
                    if rng_new <= p.cons_range_atr_mult * atr_ref_new:
                        resistance_new = float(cons["h"].max())
                        support_new = float(cons["l"].min())
                        touches_new = _touch_count(cons, resistance_new, atr_ref_new, p.resistance_touch_atr)
                        if touches_new >= p.min_resistance_touches:
                            new_end_ts = cons["ts"].iloc[-1]
                            cur_end_ts = pd.to_datetime(self.state.cons_end_ts, utc=True, errors="coerce") if self.state.cons_end_ts else None
                            if cur_end_ts is None or (new_end_ts is not pd.NaT and new_end_ts > cur_end_ts):
                                self.state.cons_start_ts = cons["ts"].iloc[0].isoformat()
                                self.state.cons_end_ts = new_end_ts.isoformat()
                                self.state.resistance = resistance_new
                                self.state.support = support_new
                                self.state.atr_ref = atr_ref_new
                                self._log(
                                    "setup_updated",
                                    reason="rolling_consolidation_refresh",
                                    resistance=resistance_new,
                                    support=support_new,
                                    atr_ref=atr_ref_new,
                                    cons_range=rng_new,
                                    resistance_touches=touches_new,
                                )


            # If we wait too long for a breakout, drop this setup and look for a fresher consolidation.
            if self.state.cons_end_ts:
                cons_end_ts = pd.to_datetime(self.state.cons_end_ts, utc=True, errors="coerce")
                if cons_end_ts is not None:
                    bars_since_cons = int((hist["ts"] > cons_end_ts).sum())
                    if bars_since_cons > p.breakout_window:
                        self._reset(
                            "breakout_timeout",
                            bars_since_cons=bars_since_cons,
                            breakout_window=p.breakout_window,
                            resistance=self.state.resistance,
                            support=self.state.support,
                        )
                        return None

            close = float(bar["c"])
            breakout_level = self.state.resistance + p.breakout_buffer_atr * self.state.atr_ref
            if close >= breakout_level:
                self.state.breakout_ts = bar["ts"].isoformat()
                self.state.breakout_high = float(bar["h"])
                self._transition(
                    "WAIT_RETEST",
                    "breakout_confirmed",
                    breakout_close=close,
                    breakout_level=breakout_level,
                    breakout_high=self.state.breakout_high,
                    resistance=self.state.resistance,
                )
            return None

        # -------------------
        # Stage 2: retest bar
        # -------------------
        if self.state.stage == "WAIT_RETEST":
            if not (self.state.resistance and self.state.atr_ref and self.state.breakout_ts):
                self._reset("missing_breakout_fields")
                return None

            # If too many bars since breakout -> reset
            breakout_ts = pd.to_datetime(self.state.breakout_ts, utc=True, errors="coerce")
            if breakout_ts is not None and len(hist) >= 2:
                # approximate bars since breakout by counting timestamps
                bars_since_breakout = int((hist["ts"] > breakout_ts).sum())
                if bars_since_breakout > p.retest_window:
                    self._reset("retest_timeout", bars_since_breakout=bars_since_breakout)
                    return None

            low = float(bar["l"])
            # If we wait too long for a breakout, drop this setup and look for a fresher consolidation.
            if self.state.cons_end_ts:
                cons_end_ts = pd.to_datetime(self.state.cons_end_ts, utc=True, errors="coerce")
                if cons_end_ts is not None:
                    bars_since_cons = int((hist["ts"] > cons_end_ts).sum())
                    if bars_since_cons > p.breakout_window:
                        self._reset(
                            "breakout_timeout",
                            bars_since_cons=bars_since_cons,
                            breakout_window=p.breakout_window,
                            resistance=self.state.resistance,
                            support=self.state.support,
                        )
                        return None

            close = float(bar["c"])
            res = float(self.state.resistance)
            atr_ref = float(self.state.atr_ref)

            wick_ok = low <= (res + p.retest_wick_atr * atr_ref)
            close_ok = close >= (res - p.retest_close_atr * atr_ref)

            if wick_ok and close_ok:
                self.state.retest_ts = bar["ts"].isoformat()
                self.state.retest_low = low

                # swing_high = max high from breakout->retest inclusive
                breakout_idx = hist.index[hist["ts"] == pd.to_datetime(self.state.breakout_ts)].tolist()
                if breakout_idx:
                    start_idx = int(breakout_idx[0])
                else:
                    start_idx = max(0, len(hist) - (p.retest_window + 2))
                swing_high = float(hist.iloc[start_idx:]["h"].max())
                self.state.swing_high = swing_high

                self._transition(
                    "WAIT_RESUMPTION",
                    "retest_confirmed",
                    retest_low=low,
                    retest_close=close,
                    resistance=res,
                    swing_high=swing_high,
                )
            return None

        # -----------------------
        # Stage 3: resumption bar
        # -----------------------
        if self.state.stage == "WAIT_RESUMPTION":
            if not (self.state.swing_high and self.state.atr_ref and self.state.retest_ts):
                self._reset("missing_retest_fields")
                return None

            # If we wait too long for a breakout, drop this setup and look for a fresher consolidation.
            if self.state.cons_end_ts:
                cons_end_ts = pd.to_datetime(self.state.cons_end_ts, utc=True, errors="coerce")
                if cons_end_ts is not None:
                    bars_since_cons = int((hist["ts"] > cons_end_ts).sum())
                    if bars_since_cons > p.breakout_window:
                        self._reset(
                            "breakout_timeout",
                            bars_since_cons=bars_since_cons,
                            breakout_window=p.breakout_window,
                            resistance=self.state.resistance,
                            support=self.state.support,
                        )
                        return None

            close = float(bar["c"])
            atr_ref = float(self.state.atr_ref)
            trigger_level = float(self.state.swing_high) + p.resume_buffer_atr * atr_ref

            # Optional quality filters
            rsi_ok = True
            if p.require_rsi_above is not None and "rsi" in bar and pd.notna(bar.get("rsi")):
                rsi_ok = float(bar["rsi"]) >= float(p.require_rsi_above)

            macd_ok = True
            if p.require_macd_hist_above is not None and "macd_hist" in bar and pd.notna(bar.get("macd_hist")):
                macd_ok = float(bar["macd_hist"]) >= float(p.require_macd_hist_above)

            ema_ok = True
            if p.require_close_above_ema21 and "ema21" in bar and pd.notna(bar.get("ema21")):
                ema_ok = close >= float(bar["ema21"])

            if close >= trigger_level and rsi_ok and macd_ok and ema_ok:
                signal_ts = bar["ts"].isoformat()

                # Dedupe: never emit twice for the same bar
                if self.state.last_signal_ts == signal_ts:
                    return None

                payload = {
                    "ts": signal_ts,
                    "signal": "BUY",
                    "pattern": "consolidation_resistance_breakout_retest_resumption",
                    "entry": close,
                    "levels": {
                        "resistance": self.state.resistance,
                        "support": self.state.support,
                        "swing_high": self.state.swing_high,
                        "retest_low": self.state.retest_low,
                        "atr_ref": self.state.atr_ref,
                    },
                    "quality": {
                        "rsi": float(bar["rsi"]) if "rsi" in bar and pd.notna(bar.get("rsi")) else None,
                        "macd_hist": float(bar["macd_hist"]) if "macd_hist" in bar and pd.notna(bar.get("macd_hist")) else None,
                        "ema21": float(bar["ema21"]) if "ema21" in bar and pd.notna(bar.get("ema21")) else None,
                    },
                }

                if not in_cooldown:
                    self.state.last_signal_ts = signal_ts
                    self.state.cooldown_remaining = int(p.cooldown_bars_after_signal)
                    self._log("signal_emitted", **payload)
                    # after signal, reset to look for next setup
                    self._reset("signal_emitted_reset")
                    return payload

            return None

        # Unknown stage -> reset
        self._reset("unknown_stage")
        return None


# -----------------------------
# Trigger outputs
# -----------------------------

def post_to_local_bot_webhook(signal: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Triggers the existing Flask bot (/webhook), which then routes to n8n AI.

    This mirrors the scheduler's overseer trigger pattern.
    """
    tv_port = int(os.getenv("TV_PORT", "5000"))
    secret = os.getenv("WEBHOOK_SECRET", "")
    if not secret:
        return False, "WEBHOOK_SECRET not set; cannot call local /webhook"

    account = os.getenv("SCANNER_ACCOUNT", "alpha")
    symbol = os.getenv("SCANNER_SYMBOL", "MES")
    size = int(os.getenv("SCANNER_SIZE", "1"))

    # You can pass extra fields; your bot currently ignores unknown keys.
    payload = {
        "secret": secret,
        "strategy": "simple",
        "account": account,
        "signal": "BUY",
        "symbol": symbol,
        "size": size,
        "alert": f"Scanner LONG: {signal.get('pattern')} @ {signal.get('entry')}",
        "scanner": signal,  # <-- useful for n8n if you decide to pass-through in ai_trade_decision
    }

    url = f"http://localhost:{tv_port}/webhook"
    try:
        resp = requests.post(url, json=payload, timeout=10)
        ok = 200 <= resp.status_code < 300
        return ok, f"local_webhook_status={resp.status_code} body={resp.text[:180]}"
    except Exception as exc:
        return False, f"local_webhook_error={exc}"


def post_to_n8n(signal: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Posts directly to an n8n webhook (optional).
    """
    url = os.getenv("N8N_SCANNER_URL", "").strip()
    if not url:
        return False, "N8N_SCANNER_URL not set"
    try:
        resp = requests.post(url, json=signal, timeout=20)
        ok = 200 <= resp.status_code < 300
        return ok, f"n8n_status={resp.status_code} body={resp.text[:180]}"
    except Exception as exc:
        return False, f"n8n_error={exc}"


# -----------------------------
# CLI / runner
# -----------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def build_params_from_env() -> ScannerParams:
    return ScannerParams(
        cons_len=_env_int("SCANNER_CONS_LEN", 12),
        cons_range_atr_mult=_env_float("SCANNER_CONS_RANGE_ATR_MULT", 2.0),
        breakout_buffer_atr=_env_float("SCANNER_BREAKOUT_BUFFER_ATR", 0.10),
        breakout_window=_env_int("SCANNER_BREAKOUT_WINDOW", 8),
        retest_window=_env_int("SCANNER_RETEST_WINDOW", 8),
        resume_window=_env_int("SCANNER_RESUME_WINDOW", 8),
    )


def run_once_from_df(df: pd.DataFrame, *, params: ScannerParams) -> List[Dict[str, Any]]:
    """
    Stateless one-shot detection over the last bars:
    - we run the state machine over the DataFrame and return any signals emitted.
    """
    state_path = Path("/tmp/_scanner_state_once.json")
    events_path = Path("/tmp/_scanner_events_once.jsonl")
    # ensure no stale state
    try:
        state_path.unlink(missing_ok=True)  # py3.8+ supports missing_ok
    except TypeError:
        if state_path.exists():
            state_path.unlink()
    try:
        events_path.unlink(missing_ok=True)
    except TypeError:
        if events_path.exists():
            events_path.unlink()

    scanner = BreakoutRetestResumptionScanner(params=params, state_path=state_path, events_path=events_path)
    return scanner.process_bars(df)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="", help="Optional CSV path (dev/backtest). If set, Supabase is not used.")
    ap.add_argument("--once", action="store_true", help="Run once and exit (no polling).")
    ap.add_argument("--emit", choices=["none", "local", "n8n"], default="none", help="Where to send signals.")
    args = ap.parse_args()

    params = build_params_from_env()

    state_path = Path(os.getenv("SCANNER_STATE_PATH", "/tmp/market_scanner_state.json"))
    events_path = Path(os.getenv("SCANNER_EVENTS_PATH", "/tmp/market_scanner_events.jsonl"))

    scanner = BreakoutRetestResumptionScanner(params=params, state_path=state_path, events_path=events_path)

    table = os.getenv("SCANNER_TABLE", "tv_datafeed_30m")
    symbol = os.getenv("SCANNER_SYMBOL", "MES")
    timeframe = os.getenv("SCANNER_TIMEFRAME")  # optional; e.g. "30" or "30m"
    lookback = _env_int("SCANNER_LOOKBACK", 200)
    poll_seconds = _env_int("SCANNER_POLL_SECONDS", 20)

    def fetch_df() -> pd.DataFrame:
        if args.csv:
            return load_bars_from_csv(args.csv, limit=lookback)
        return fetch_bars_from_supabase_rest(table=table, symbol=symbol, timeframe=timeframe, limit=lookback)

    if args.once:
        df = fetch_df()
        sigs = run_once_from_df(df.tail(60).copy(), params=params)
        print(json.dumps({"signals": sigs}, indent=2))
        return 0

    while True:
        try:
            df = fetch_df()
            sigs = scanner.process_bars(df)
            for sig in sigs:
                if args.emit == "local":
                    ok, msg = post_to_local_bot_webhook(sig)
                    scanner._log("signal_delivery", destination="local_webhook", ok=ok, message=msg)
                elif args.emit == "n8n":
                    ok, msg = post_to_n8n(sig)
                    scanner._log("signal_delivery", destination="n8n", ok=ok, message=msg)
                else:
                    scanner._log("signal_delivery", destination="none", ok=True, message="signal generated but not emitted")
        except KeyboardInterrupt:
            print("Exiting on Ctrl+C")
            return 0
        except Exception as exc:
            append_jsonl(events_path, {"ts": _utc_now_iso(), "event": "scanner_error", "error": str(exc)})
        time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
