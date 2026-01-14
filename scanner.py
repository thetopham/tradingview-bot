'''
# scanner.py
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple
import logging
import os
import time

import numpy as np
import pandas as pd
import requests

from config import load_config
from auth import in_get_flat


# ----------------------------- Core detector -----------------------------

def _round_tick(x: float, tick: float = 0.25) -> float:
    return round(x / tick) * tick


def _pick_level_near_price(
    look: pd.DataFrame,
    current_price: float,
    direction: str,
    band: float,
    tick: float,
    touch_tol: float
) -> tuple[Optional[float], int]:
    """
    Pick the strongest level near current price by touch-count.
    LONG: level from clustered highs (resistance)
    SHORT: level from clustered lows (support)
    """
    direction = direction.upper()
    if direction == "LONG":
        vals = look["h"].to_numpy()
    else:
        vals = look["l"].to_numpy()

    vals = vals[(vals >= current_price - band) & (vals <= current_price + band)]
    if len(vals) == 0:
        return None, 0

    lvls = np.round(vals / tick) * tick
    uniq = np.unique(lvls)

    highs = look["h"].to_numpy()
    lows = look["l"].to_numpy()

    best_lvl: Optional[float] = None
    best_cnt = 0

    for lvl in uniq:
        if direction == "LONG":
            cnt = int(np.sum(np.abs(highs - lvl) <= touch_tol))
        else:
            cnt = int(np.sum(np.abs(lows - lvl) <= touch_tol))

        if cnt > best_cnt:
            best_cnt = cnt
            best_lvl = float(lvl)

    return best_lvl, best_cnt


@dataclass
class LevelBRCParams:
    # Level selection
    level_lookback: int = 500
    level_band: float = 25.0
    level_touch_tol: float = 1.0
    min_touches: int = 3
    tick: float = 0.25

    # Breakout phase: require N consecutive closes beyond level
    breakout_min_close: float = 0.0
    breakout_min_bars_above: int = 6
    breakout_max_bars: int = 80

    # Retest phase: touch level area, don't fail
    retest_touch_tol: float = 1.25
    retest_fail_close: float = 2.0
    retest_max_bars: int = 40

    # Resume trigger: early or confirmed
    resume_trigger_mode: str = "LEVEL_RECLAIM"  # LEVEL_RECLAIM or PIVOT_BREAK
    pivot_lookback: int = 6

    # Filters
    ema_filter: bool = True  # LONG: ema9>ema21 and close>=ema9; SHORT inverse


@dataclass
class LevelBRCState:
    phase: str = "SCAN_LEVEL"  # SCAN_LEVEL | WAIT_BREAKOUT | WAIT_RETEST | WAIT_RESUME
    direction: Optional[str] = None
    level: Optional[float] = None
    level_set_idx: Optional[int] = None
    breakout_start_idx: Optional[int] = None
    retest_start_idx: Optional[int] = None
    last_signal_ts: Optional[str] = None


def detect_level_brc(
    bars: List[Dict[str, Any]],
    prev_state: Optional[Dict[str, Any]],
    direction: str,
    p: LevelBRCParams
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:

    st = LevelBRCState(**prev_state) if prev_state else LevelBRCState()
    direction = direction.upper().strip()
    if direction not in ("LONG", "SHORT"):
        raise ValueError("direction must be LONG or SHORT")

    # Supabase returns ts DESC typically; we will sort ASC here
    bars = sorted(bars, key=lambda b: b.get("ts", ""))
    i = len(bars) - 1
    if i < max(p.level_lookback, 50):
        return asdict(st), None

    cur = bars[i]
    o, h, l, c = float(cur["o"]), float(cur["h"]), float(cur["l"]), float(cur["c"])
    ts = str(cur.get("ts", ""))

    ema9 = float(cur.get("ema9", np.nan))
    ema21 = float(cur.get("ema21", np.nan))

    def ema_ok() -> bool:
        if not p.ema_filter or np.isnan(ema9) or np.isnan(ema21):
            return True
        if direction == "LONG":
            return (ema9 > ema21) and (c >= ema9)
        return (ema9 < ema21) and (c <= ema9)

    def reset():
        nonlocal st
        st = LevelBRCState(phase="SCAN_LEVEL", last_signal_ts=st.last_signal_ts)

    # 1) Choose a strong level once
    if st.phase == "SCAN_LEVEL":
        look = pd.DataFrame(bars[i - p.level_lookback : i])  # exclude current
        lvl, touches = _pick_level_near_price(
            look=look,
            current_price=c,
            direction=direction,
            band=p.level_band,
            tick=p.tick,
            touch_tol=p.level_touch_tol,
        )

        if lvl is not None and touches >= p.min_touches:
            st.phase = "WAIT_BREAKOUT"
            st.direction = direction
            st.level = _round_tick(lvl, p.tick)
            st.level_set_idx = i - 1
            st.breakout_start_idx = None
            st.retest_start_idx = None

        return asdict(st), None

    # Keep separate state per direction in practice.
    if st.direction != direction:
        return asdict(st), None

    level = st.level
    if level is None:
        reset()
        return asdict(st), None

    # 2) Breakout hold
    if st.phase == "WAIT_BREAKOUT":
        if st.level_set_idx is not None and (i - st.level_set_idx) > p.breakout_max_bars:
            reset()
            return asdict(st), None

        ok = (c >= level + p.breakout_min_close) if direction == "LONG" else (c <= level - p.breakout_min_close)
        if ok:
            consec = 0
            j = i
            while j >= 0:
                cj = float(bars[j]["c"])
                okj = (cj >= level + p.breakout_min_close) if direction == "LONG" else (cj <= level - p.breakout_min_close)
                if not okj:
                    break
                consec += 1
                j -= 1

            if consec >= p.breakout_min_bars_above:
                st.phase = "WAIT_RETEST"
                st.breakout_start_idx = j + 1

        return asdict(st), None

    # fail after breakout
    fail = (c < level - p.retest_fail_close) if direction == "LONG" else (c > level + p.retest_fail_close)
    if fail:
        reset()
        return asdict(st), None

    # 3) Retest touch
    if st.phase == "WAIT_RETEST":
        if st.breakout_start_idx is not None and (i - st.breakout_start_idx) > p.retest_max_bars:
            reset()
            return asdict(st), None

        touch = (l <= level + p.retest_touch_tol) if direction == "LONG" else (h >= level - p.retest_touch_tol)
        if touch:
            st.phase = "WAIT_RESUME"
            st.retest_start_idx = i

        return asdict(st), None

    # 4) Resume trigger
    if st.phase == "WAIT_RESUME":
        if st.retest_start_idx is not None and (i - st.retest_start_idx) > p.retest_max_bars:
            reset()
            return asdict(st), None

        if not ema_ok():
            return asdict(st), None

        if p.resume_trigger_mode == "LEVEL_RECLAIM":
            trigger = (c >= level) if direction == "LONG" else (c <= level)
        else:
            lb = p.pivot_lookback
            if i - lb < 0:
                return asdict(st), None
            recent = bars[i - lb : i]
            if direction == "LONG":
                pivot = max(float(b["h"]) for b in recent)
                trigger = c > pivot
            else:
                pivot = min(float(b["l"]) for b in recent)
                trigger = c < pivot

        already = (st.last_signal_ts == ts and ts != "")
        if trigger and not already:
            ev = {
                "setup": f"LEVEL_BRC_{direction}",
                "signal": "BUY" if direction == "LONG" else "SELL",
                "ts": ts,
                "bar_index": i,
                "level": float(level),
                "entry": float(c),
                "invalidation": float(level - p.retest_fail_close) if direction == "LONG" else float(level + p.retest_fail_close),
                "params": {
                    "resume_trigger_mode": p.resume_trigger_mode,
                    "breakout_min_bars_above": p.breakout_min_bars_above,
                    "retest_fail_close": p.retest_fail_close,
                    "retest_touch_tol": p.retest_touch_tol,
                    "level_touch_tol": p.level_touch_tol,
                    "min_touches": p.min_touches,
                }
            }
            st.last_signal_ts = ts
            reset()
            return asdict(st), ev

        return asdict(st), None

    return asdict(st), None


# ----------------------------- Supabase fetch -----------------------------

class SupabaseFeed:
    def __init__(self, supabase_url: str, supabase_key: str, session: Optional[requests.Session] = None):
        self.supabase_url = (supabase_url or "").rstrip("/")
        self.supabase_key = supabase_key or ""
        self.session = session or requests.Session()

    def fetch_bars(
        self,
        table: str,
        symbol: str,
        limit: int,
        select: str = "ts,o,h,l,c,v,ema9,ema21,vwap,atr,macd_hist",
    ) -> List[Dict[str, Any]]:
        if not self.supabase_url or not self.supabase_key:
            return []

        url = f"{self.supabase_url}/rest/v1/{table}"
        params = {
            "symbol": f"eq.{symbol}",
            "select": select,
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


# ----------------------------- Market scanner -----------------------------

class MarketScanner:
    """
    Runs the level-based BRC detector on tv_datafeed_<tf>.
    When a setup triggers, it posts to local Flask /webhook with scanner payload.
    """
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.mt = config["MT"]
        self.webhook_secret = config["WEBHOOK_SECRET"]
        self.tv_port = config["TV_PORT"]

        self.symbol = os.getenv("SCANNER_SYMBOL", "MES")
        self.table_5m = os.getenv("SCANNER_TABLE_5M", "tv_datafeed_5m")

        # Which account should receive scanner-triggered webhook?
        # Default: epsilon if available, else DEFAULT_ACCOUNT
        default_acct = "epsilon" if "epsilon" in config["ACCOUNTS"] else config["DEFAULT_ACCOUNT"]
        self.account = os.getenv("SCANNER_ACCOUNT", default_acct).lower()

        # Entry style
        mode = os.getenv("SCANNER_RESUME_MODE", "LEVEL_RECLAIM").upper()
        self.params = LevelBRCParams(
            resume_trigger_mode="PIVOT_BREAK" if mode == "PIVOT_BREAK" else "LEVEL_RECLAIM"
        )

        # Internal states (keep separate for long/short)
        self.state_long: Dict[str, Any] = {}
        self.state_short: Dict[str, Any] = {}

        self.feed = SupabaseFeed(config.get("SUPABASE_URL"), config.get("SUPABASE_KEY"))

        # debounce so we don’t spam
        self.last_emitted_ts: Optional[str] = None

    def scan_5m_once(self) -> Optional[Dict[str, Any]]:
        bars = self.feed.fetch_bars(
            table=self.table_5m,
            symbol=self.symbol,
            limit=max(self.params.level_lookback + 10, 650),
        )
        if not bars:
            logging.info("[Scanner] No bars returned from %s", self.table_5m)
            return None

        # run LONG then SHORT
        self.state_long, ev_long = detect_level_brc(
            bars=bars,
            prev_state=self.state_long,
            direction="LONG",
            p=self.params,
        )
        self.state_short, ev_short = detect_level_brc(
            bars=bars,
            prev_state=self.state_short,
            direction="SHORT",
            p=self.params,
        )

        ev = ev_long or ev_short
        if not ev:
            return None

        ts = str(ev.get("ts", ""))
        if ts and self.last_emitted_ts == ts:
            return None
        self.last_emitted_ts = ts
        return ev

    def emit_to_local_webhook(self, event: Dict[str, Any]) -> bool:
        """
        Sends a webhook into your existing Flask /webhook so it flows into n8n.
        We intentionally set signal="HOLD" and let n8n/AI decide using scanner context.
        """
        payload = {
            "secret": self.webhook_secret,
            "strategy": "simple",
            "account": self.account,
            "signal": "HOLD",
            "symbol": self.config.get("OVERRIDE_CONTRACT_ID") or "CON.F.US.MES.H26",
            "size": 1,
            "alert": f"Scanner {event.get('setup')} @ {event.get('ts')}",
            "scanner": event,  # <--- critical: flows into n8n payload
        }

        url = f"http://localhost:{self.tv_port}/webhook"
        try:
            resp = requests.post(url, json=payload, timeout=10)
            logging.info("[Scanner] emitted %s status=%s body=%s", event.get("setup"), resp.status_code, resp.text[:120])
            return resp.ok
        except Exception as exc:
            logging.error("[Scanner] emit failed: %s", exc)
            return False

    def run_scan_and_trigger(self):
        now = pd.Timestamp.now(tz=self.mt)
        if in_get_flat(now.to_pydatetime()):
            logging.info("[Scanner] In get-flat window; skipping")
            return

        if self.account not in self.config["ACCOUNTS"]:
            logging.warning("[Scanner] account=%s not in ACCOUNTS; skipping", self.account)
            return

        ev = self.scan_5m_once()
        if ev:
            self.emit_to_local_webhook(ev)
