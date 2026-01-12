# simbroker_assistant.py
"""
SimBroker (assistant implementation)

A local, persistent broker simulator that emulates the ProjectX Gateway REST API
response shapes closely enough to be a drop-in replacement for this repo.

Key features:
- Multiple simulated accounts (sim001, sim002, ...) with per-account bracket rules.
- Broker-side bracket simulation (SL/TP) in USD, converted to price using tickSize/tickValue.
- Deterministic replay via last_processed_bar_ts per (accountId, contractId).
- JSON persistence with atomic writes and a process-level lock.

This module intentionally does NOT import api.py to avoid circular imports.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from dateutil import parser as dtparser

log = logging.getLogger(__name__)

# --- ProjectX enums (from ProjectX Gateway API.txt) -------------------------
ORDER_SIDE_BID = 0  # Buy
ORDER_SIDE_ASK = 1  # Sell

ORDER_TYPE_LIMIT = 1
ORDER_TYPE_MARKET = 2
ORDER_TYPE_STOP = 4

ORDER_STATUS_OPEN = 1
ORDER_STATUS_FILLED = 2
ORDER_STATUS_CANCELLED = 3
ORDER_STATUS_REJECTED = 5

POSITION_TYPE_LONG = 1
POSITION_TYPE_SHORT = 2


# --- Utilities --------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts: Any) -> Optional[datetime]:
    """Parse timestamp strings robustly (supports '2026-01-02 04:05:...' and ISO)."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        s = str(ts).strip()
        if not s:
            return None
        dt = dtparser.parse(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _round_to_tick(price: float, tick_size: float) -> float:
    if not tick_size or tick_size <= 0:
        return float(price)
    return round(round(price / tick_size) * tick_size, 10)


def _symbol_id_from_contract_id(contract_id: str) -> Optional[str]:
    # Example: CON.F.US.MES.H26 -> F.US.MES
    try:
        parts = str(contract_id).split(".")
        if len(parts) >= 4:
            return ".".join(parts[1:4])
    except Exception:
        pass
    return None


def _root_symbol_from_contract_id(contract_id: str) -> str:
    # Example: CON.F.US.MES.H26 -> MES
    try:
        parts = str(contract_id).split(".")
        if len(parts) >= 4:
            return parts[3]
    except Exception:
        pass

    # fallback heuristic
    for root in ("MES", "ES", "MNQ", "NQ", "MYM", "YM", "MCL", "CL", "MGC", "GC"):
        if root in str(contract_id):
            return root
    return os.getenv("SIM_DEFAULT_SYMBOL", "MES")


def _default_contract_specs(root: str) -> Tuple[float, float]:
    """Return (tickSize, tickValue) defaults by root symbol."""
    root = (root or "").upper()
    mapping = {
        "MES": (0.25, 1.25),
        "ES": (0.25, 12.5),
        "MNQ": (0.25, 0.5),
        "NQ": (0.25, 5.0),
        "MYM": (1.0, 0.5),
        "YM": (1.0, 5.0),
        "MCL": (0.01, 1.0),
        "CL": (0.01, 10.0),
        "MGC": (0.1, 1.0),
        "GC": (0.1, 10.0),
    }
    return mapping.get(root, (0.25, 1.25))


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return float(default)
    try:
        return float(str(raw).strip())
    except Exception:
        return float(default)


def _env_str(key: str, default: str) -> str:
    raw = os.getenv(key)
    if raw is None:
        return default
    return str(raw).strip() or default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _wrap_success(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload)
    out.setdefault("success", True)
    out.setdefault("errorCode", 0)
    out.setdefault("errorMessage", None)
    return out


def _wrap_error(message: str, code: int = 1) -> Dict[str, Any]:
    return {"success": False, "errorCode": int(code), "errorMessage": str(message)}


# --- Bar feed abstractions --------------------------------------------------


@dataclass(frozen=True)
class Bar:
    ts: str  # ISO timestamp
    o: float
    h: float
    l: float
    c: float
    v: float


class BarFeed:
    """Abstract price feed used by SimBroker."""

    def latest_bar(self, symbol: str, timeframe_filters: List[str]) -> Optional[Bar]:
        raise NotImplementedError

    def bars_between(
        self,
        symbol: str,
        timeframe_filters: List[str],
        start_ts_exclusive: Optional[str],
        end_ts_inclusive: Optional[str],
        limit: int = 5000,
        order: str = "asc",
    ) -> List[Bar]:
        raise NotImplementedError


class SupabaseBarFeed(BarFeed):
    """
    Pulls OHLCV from Supabase via PostgREST.

    Expected tables:
      - tv_datafeed (recommended) with columns: symbol, timeframe, ts, o, h, l, c, v
    """

    def __init__(self, *, supabase_url: str, supabase_key: str, table: str = "tv_datafeed"):
        self.supabase_url = supabase_url.rstrip("/")
        self.supabase_key = supabase_key
        self.table = table
        self.session = requests.Session()

    def _get(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        url = f"{self.supabase_url}/rest/v1/{self.table}"
        headers = {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
        }
        resp = self.session.get(url, headers=headers, params=params, timeout=(3.05, 10))
        if resp.status_code >= 400:
            raise RuntimeError(f"Supabase GET failed {resp.status_code}: {resp.text[:300]}")
        return resp.json() or []

    def latest_bar(self, symbol: str, timeframe_filters: List[str]) -> Optional[Bar]:
        tf_clause = ",".join(f"timeframe.eq.\"{tf}\"" for tf in timeframe_filters)
        rows = self._get(
            {
                "select": "ts,o,h,l,c,v",
                "symbol": f"eq.{symbol}",
                "or": f"({tf_clause})",
                "order": "ts.desc",
                "limit": 1,
            }
        )
        if not rows:
            return None
        r = rows[0]
        ts = _parse_ts(r.get("ts"))
        if not ts:
            return None
        return Bar(
            ts=_iso(ts),
            o=float(r.get("o")),
            h=float(r.get("h")),
            l=float(r.get("l")),
            c=float(r.get("c")),
            v=float(r.get("v") or 0),
        )

    def bars_between(
        self,
        symbol: str,
        timeframe_filters: List[str],
        start_ts_exclusive: Optional[str],
        end_ts_inclusive: Optional[str],
        limit: int = 5000,
        order: str = "asc",
    ) -> List[Bar]:
        tf_clause = ",".join(f"timeframe.eq.\"{tf}\"" for tf in timeframe_filters)
        params: Dict[str, Any] = {
            "select": "ts,o,h,l,c,v",
            "symbol": f"eq.{symbol}",
            "or": f"({tf_clause})",
            "order": f"ts.{order}",
            "limit": int(limit),
        }
        if start_ts_exclusive:
            params["ts"] = f"gt.{start_ts_exclusive}"
        if end_ts_inclusive:
            # PostgREST doesn't support two filters on same field with plain params,
            # but it supports an "and" clause; easiest is to filter client-side.
            pass

        rows = self._get(params)
        bars: List[Bar] = []
        for r in rows:
            ts = _parse_ts(r.get("ts"))
            if not ts:
                continue
            iso_ts = _iso(ts)
            if end_ts_inclusive:
                end_dt = _parse_ts(end_ts_inclusive)
                if end_dt and ts > end_dt:
                    continue
            try:
                bars.append(
                    Bar(
                        ts=iso_ts,
                        o=float(r.get("o")),
                        h=float(r.get("h")),
                        l=float(r.get("l")),
                        c=float(r.get("c")),
                        v=float(r.get("v") or 0),
                    )
                )
            except Exception:
                continue

        # Ensure ordering is correct even if server didn't return strict order
        bars.sort(key=lambda b: _parse_ts(b.ts) or datetime.min.replace(tzinfo=timezone.utc))
        if order == "desc":
            bars.reverse()
        return bars


class CSVBarFeed(BarFeed):
    """
    Minimal CSV bar loader (fallback mode).

    You can point SIM_CSV_FEED_PATH to any CSV that has at least:
      ts,o,h,l,c,v,symbol,timeframe

    This repo's exports (tv_datafeed_5m_rows*.csv, etc) are compatible.
    """

    def __init__(self, csv_path: str):
        self.csv_path = str(csv_path)
        self._loaded = False
        self._bars: Dict[Tuple[str, str], List[Bar]] = {}
        self._lock = threading.RLock()

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            path = Path(self.csv_path)
            if not path.exists():
                raise FileNotFoundError(f"CSV feed not found: {path}")
            with path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = str(row.get("symbol") or "").strip()
                    tf = str(row.get("timeframe") or "").strip()
                    if not sym or not tf:
                        continue
                    ts = _parse_ts(row.get("ts"))
                    if not ts:
                        continue
                    try:
                        bar = Bar(
                            ts=_iso(ts),
                            o=float(row.get("o")),
                            h=float(row.get("h")),
                            l=float(row.get("l")),
                            c=float(row.get("c")),
                            v=float(row.get("v") or 0),
                        )
                    except Exception:
                        continue
                    self._bars.setdefault((sym, tf), []).append(bar)

            # sort each stream
            for k in list(self._bars.keys()):
                self._bars[k].sort(key=lambda b: _parse_ts(b.ts) or datetime.min.replace(tzinfo=timezone.utc))

            self._loaded = True
            log.info("CSVBarFeed loaded %s streams from %s", len(self._bars), path)

    def latest_bar(self, symbol: str, timeframe_filters: List[str]) -> Optional[Bar]:
        self._load()
        with self._lock:
            for tf in timeframe_filters:
                stream = self._bars.get((symbol, str(tf)))
                if stream:
                    return stream[-1]
        return None

    def bars_between(
        self,
        symbol: str,
        timeframe_filters: List[str],
        start_ts_exclusive: Optional[str],
        end_ts_inclusive: Optional[str],
        limit: int = 5000,
        order: str = "asc",
    ) -> List[Bar]:
        self._load()
        start_dt = _parse_ts(start_ts_exclusive) if start_ts_exclusive else None
        end_dt = _parse_ts(end_ts_inclusive) if end_ts_inclusive else None

        candidates: List[Bar] = []
        with self._lock:
            for tf in timeframe_filters:
                stream = self._bars.get((symbol, str(tf)))
                if not stream:
                    continue
                for b in stream:
                    bdt = _parse_ts(b.ts)
                    if not bdt:
                        continue
                    if start_dt and bdt <= start_dt:
                        continue
                    if end_dt and bdt > end_dt:
                        continue
                    candidates.append(b)

                if candidates:
                    break  # first tf with data wins

        candidates.sort(key=lambda b: _parse_ts(b.ts) or datetime.min.replace(tzinfo=timezone.utc))
        if order == "desc":
            candidates.reverse()
        return candidates[: int(limit)]


# --- Account config loader --------------------------------------------------


def _load_sim_accounts_file() -> Dict[str, Dict[str, Any]]:
    """
    Optional external config for many accounts.

    Set SIM_ACCOUNTS_FILE to a JSON file path.

    Accepts either:
      A) {"sim001": {"id": 900001, "balance": 50000, "sl_usd": 30, "tp_usd": 60}, ...}
      B) [{"name":"sim001","id":900001,"balance":50000,"sl_usd":30,"tp_usd":60}, ...]
    """
    path = _env_str("SIM_ACCOUNTS_FILE", "").strip()
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except Exception as exc:
        log.warning("Failed to parse SIM_ACCOUNTS_FILE %s: %s", p, exc)
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(data, dict):
        for name, cfg in data.items():
            if not name:
                continue
            if isinstance(cfg, dict):
                out[str(name).lower()] = dict(cfg)
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip().lower()
            if not name:
                continue
            out[name] = dict(item)
    return out


def _load_sim_account_rules_json() -> Dict[str, Dict[str, Any]]:
    raw = os.getenv("SIM_ACCOUNT_RULES_JSON") or os.getenv("SIM_ACCOUNTS_JSON")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for name, cfg in data.items():
            if isinstance(cfg, dict):
                out[str(name).lower()] = dict(cfg)
        return out
    except Exception:
        return {}


@dataclass
class BracketRule:
    sl_usd: float
    tp_usd: float
    fill_policy: str = "worst"  # "worst" or "best"


@dataclass
class AccountSpec:
    id: int
    name: str
    balance: float
    canTrade: bool = True
    isVisible: bool = True
    simulated: bool = True
    bracket: Optional[BracketRule] = None


def _resolve_account_spec(name: str, account_id: int) -> AccountSpec:
    """
    Resolve per-account rules.

    Priority (highest -> lowest):
      1) SIM_ACCOUNTS_FILE entry for this account
      2) SIM_ACCOUNT_RULES_JSON / SIM_ACCOUNTS_JSON entry for this account
      3) Per-account env vars: SIM_<NAME>_SL_USD / SIM_<NAME>_TP_USD / SIM_<NAME>_START_BALANCE
      4) Global env vars: SIM_BRACKET_SL_USD / SIM_BRACKET_TP_USD / SIM_START_BALANCE
    """
    name_l = str(name).lower()
    file_cfg = _load_sim_accounts_file().get(name_l, {})
    json_cfg = _load_sim_account_rules_json().get(name_l, {})

    def _pick_float(keys: List[str], default: float) -> float:
        for k in keys:
            if k in file_cfg:
                try:
                    return float(file_cfg[k])
                except Exception:
                    pass
            if k in json_cfg:
                try:
                    return float(json_cfg[k])
                except Exception:
                    pass
        for k in keys:
            raw = os.getenv(k)
            if raw is not None and str(raw).strip() != "":
                try:
                    return float(raw)
                except Exception:
                    pass
        return float(default)

    def _pick_str(keys: List[str], default: str) -> str:
        for k in keys:
            if k in file_cfg:
                try:
                    return str(file_cfg[k]).strip()
                except Exception:
                    pass
            if k in json_cfg:
                try:
                    return str(json_cfg[k]).strip()
                except Exception:
                    pass
        for k in keys:
            raw = os.getenv(k)
            if raw is not None and str(raw).strip() != "":
                return str(raw).strip()
        return str(default)

    name_key = re.sub(r"[^A-Za-z0-9]", "_", name).upper()

    balance = _pick_float(
        [f"SIM_{name_key}_START_BALANCE", "balance", "starting_balance"],
        _env_float("SIM_START_BALANCE", 50000.0),
    )

    sl_usd = _pick_float(
        [f"SIM_{name_key}_SL_USD", f"SIM_{name_key}_BRACKET_SL_USD", "sl_usd", "sl", "stop_loss_usd"],
        _env_float("SIM_BRACKET_SL_USD", 30.0),
    )
    tp_usd = _pick_float(
        [f"SIM_{name_key}_TP_USD", f"SIM_{name_key}_BRACKET_TP_USD", "tp_usd", "tp", "take_profit_usd"],
        _env_float("SIM_BRACKET_TP_USD", 60.0),
    )
    fill_policy = _pick_str(
        [f"SIM_{name_key}_FILL_POLICY", "fill_policy"],
        _env_str("SIM_FILL_POLICY", "worst"),
    ).lower()
    if fill_policy not in {"worst", "best"}:
        fill_policy = "worst"

    bracket = BracketRule(sl_usd=float(sl_usd), tp_usd=float(tp_usd), fill_policy=fill_policy)

    return AccountSpec(id=int(account_id), name=str(name), balance=float(balance), bracket=bracket)


# --- SimBroker --------------------------------------------------------------


class SimBroker:
    def __init__(
        self,
        *,
        state_path: Optional[str] = None,
        bar_feed: Optional[BarFeed] = None,
    ):
        self.state_path = Path(state_path or os.getenv("SIMBROKER_STATE_PATH", "./simbroker_state.json"))
        self._lock = threading.RLock()

        self.bar_feed = bar_feed or self._make_default_bar_feed()

        # load or initialize state
        self.state: Dict[str, Any] = {}
        self._load_state()

        # account name lookup (id -> name), populated via sync
        self._account_name_by_id: Dict[int, str] = {}

        # timeframe selection cache per contractId
        self._tf_by_contract: Dict[str, List[str]] = {}

        # bring accounts in sync with env / SIM_ACCOUNTS_FILE
        self.sync_accounts()

    # ---------------- persistence ----------------

    def _load_state(self) -> None:
        with self._lock:
            if self.state_path.exists():
                try:
                    self.state = json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
                except Exception as exc:
                    log.error("Failed to parse simbroker state %s: %s", self.state_path, exc)
                    self.state = {}
            else:
                self.state = {}

            # defaults
            self.state.setdefault("schema_version", 1)
            self.state.setdefault("saved_at", _utc_now_iso())
            self.state.setdefault("accounts", [])  # list[dict]
            self.state.setdefault("orders", [])  # list[dict]
            self.state.setdefault("positions", [])  # list[dict]
            self.state.setdefault("trades", [])  # list[dict]
            self.state.setdefault("brackets", {})  # key: "<acct>|<cid>" -> dict
            self.state.setdefault("last_processed_bar_ts", {})  # key -> iso ts
            self.state.setdefault("nextOrderId", 1000)
            self.state.setdefault("nextPositionId", 1000)
            self.state.setdefault("nextTradeId", 1000)

            self._save_state()

    def _save_state(self) -> None:
        with self._lock:
            self.state["saved_at"] = _utc_now_iso()
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)

    def _next_id(self, key: str) -> int:
        with self._lock:
            nxt = int(self.state.get(key, 1000))
            self.state[key] = nxt + 1
            return nxt

    # ---------------- account sync ----------------

    def sync_accounts(self) -> None:
        """
        Ensure every configured account exists in persisted state.
        Account sources:
          1) ACCOUNT_* env vars (preferred because repo already uses them)
          2) SIM_ACCOUNTS_FILE / SIM_ACCOUNTS / SIM_ACCOUNT_ID_START fallbacks (for sim mode)
        """
        with self._lock:
            # Load accounts from env (ACCOUNT_<name>=<id>)
            accounts_env: Dict[str, int] = {}
            for k, v in os.environ.items():
                if not k.startswith("ACCOUNT_"):
                    continue
                name = k[len("ACCOUNT_") :].strip().lower()
                try:
                    accounts_env[name] = int(str(v).strip())
                except Exception:
                    continue

            # Optional SIM_ACCOUNTS_FILE
            file_cfg = _load_sim_accounts_file()
            if file_cfg:
                for name, cfg in file_cfg.items():
                    try:
                        accounts_env.setdefault(name.lower(), int(cfg.get("id")))
                    except Exception:
                        continue

            # Optional SIM_ACCOUNTS list
            if not accounts_env:
                sim_list = _env_str("SIM_ACCOUNTS", "").strip()
                if sim_list:
                    start = int(_env_float("SIM_ACCOUNT_ID_START", 900000))
                    for i, name in enumerate([x.strip().lower() for x in sim_list.split(",") if x.strip()]):
                        accounts_env.setdefault(name, start + i)

            # final fallback for sim: create sim001
            if not accounts_env:
                accounts_env["sim001"] = int(_env_float("SIM_ACCOUNT_ID_START", 900000))

            # index existing accounts in state by id
            by_id: Dict[int, Dict[str, Any]] = {int(a.get("id")): a for a in (self.state.get("accounts") or []) if a.get("id") is not None}

            for name, aid in accounts_env.items():
                spec = _resolve_account_spec(name, aid)
                self._account_name_by_id[int(aid)] = name

                existing = by_id.get(int(aid))
                if existing is None:
                    acct_row = {
                        "id": int(spec.id),
                        "name": str(spec.name),
                        "balance": float(spec.balance),
                        "canTrade": bool(spec.canTrade),
                        "isVisible": bool(spec.isVisible),
                        "simulated": True,
                        "bracket": {
                            "sl_usd": float(spec.bracket.sl_usd) if spec.bracket else None,
                            "tp_usd": float(spec.bracket.tp_usd) if spec.bracket else None,
                            "fill_policy": str(spec.bracket.fill_policy) if spec.bracket else "worst",
                        },
                    }
                    self.state["accounts"].append(acct_row)
                    by_id[int(aid)] = acct_row
                    log.info("SimBroker: created account %s id=%s", name, aid)
                else:
                    # Update name + bracket rules, but do NOT overwrite balance unless requested
                    existing["name"] = str(spec.name)
                    if spec.bracket:
                        existing.setdefault("bracket", {})
                        existing["bracket"]["sl_usd"] = float(spec.bracket.sl_usd)
                        existing["bracket"]["tp_usd"] = float(spec.bracket.tp_usd)
                        existing["bracket"]["fill_policy"] = str(spec.bracket.fill_policy)

                    if _env_bool("SIM_RESET_BALANCE", False):
                        existing["balance"] = float(spec.balance)

            self._save_state()

    # ---------------- contract + pricing ----------------

    def _make_default_bar_feed(self) -> BarFeed:
        supabase_url = os.getenv("SUPABASE_URL") or ""
        supabase_key = os.getenv("SUPABASE_KEY") or ""

        preferred = _env_str("SIM_BAR_FEED", "").lower().strip()  # "supabase" or "csv" or ""
        if preferred == "csv":
            csv_path = _env_str("SIM_CSV_FEED_PATH", "")
            if not csv_path:
                raise RuntimeError("SIM_BAR_FEED=csv requires SIM_CSV_FEED_PATH")
            return CSVBarFeed(csv_path)
        if preferred == "supabase":
            if not supabase_url or not supabase_key:
                raise RuntimeError("SIM_BAR_FEED=supabase requires SUPABASE_URL and SUPABASE_KEY")
            return SupabaseBarFeed(supabase_url=supabase_url, supabase_key=supabase_key)

        # auto
        if supabase_url and supabase_key:
            return SupabaseBarFeed(supabase_url=supabase_url, supabase_key=supabase_key)

        csv_path = _env_str("SIM_CSV_FEED_PATH", "")
        if csv_path:
            return CSVBarFeed(csv_path)

        raise RuntimeError("No bar feed configured. Set SUPABASE_URL/SUPABASE_KEY or SIM_CSV_FEED_PATH.")

    def _contract_specs(self, contract_id: str) -> Tuple[float, float]:
        root = _root_symbol_from_contract_id(contract_id)
        tick_size_default, tick_value_default = _default_contract_specs(root)
        tick_size = _env_float("SIM_TICK_SIZE", tick_size_default)
        tick_value = _env_float("SIM_TICK_VALUE", tick_value_default)
        return float(tick_size), float(tick_value)

    def _preferred_timeframes(self) -> List[str]:
        # prefer 1m, then 5m (match repo conventions)
        return ["1m", "1", "5m", "5"]

    def _latest_price(self, contract_id: str) -> Tuple[Optional[float], Optional[str], Optional[str]]:
        """Return (price, iso_ts, used_timeframe)"""
        symbol = _root_symbol_from_contract_id(contract_id)
        tfs = self._tf_by_contract.get(contract_id) or self._preferred_timeframes()
        bar = self.bar_feed.latest_bar(symbol, tfs)
        if not bar and tfs != self._preferred_timeframes():
            bar = self.bar_feed.latest_bar(symbol, self._preferred_timeframes())
            tfs = self._preferred_timeframes()
        if not bar:
            return None, None, None

        # cache chosen TFs (first one that worked)
        self._tf_by_contract[contract_id] = tfs
        return float(bar.c), str(bar.ts), tfs[0]

    def _bars_since(
        self,
        contract_id: str,
        start_ts_exclusive: Optional[str],
        end_ts_inclusive: Optional[str],
        limit: int = 5000,
    ) -> List[Bar]:
        symbol = _root_symbol_from_contract_id(contract_id)
        tfs = self._tf_by_contract.get(contract_id) or self._preferred_timeframes()
        bars = self.bar_feed.bars_between(symbol, tfs, start_ts_exclusive, end_ts_inclusive, limit=limit, order="asc")
        if not bars and tfs != self._preferred_timeframes():
            bars = self.bar_feed.bars_between(symbol, self._preferred_timeframes(), start_ts_exclusive, end_ts_inclusive, limit=limit, order="asc")
            tfs = self._preferred_timeframes()
        if bars:
            self._tf_by_contract[contract_id] = tfs
        return bars

    # ---------------- bracket math ----------------

    def _bracket_for_account(self, account_id: int) -> BracketRule:
        acct = self._get_account_row(account_id)
        br = (acct or {}).get("bracket") or {}
        sl = float(br.get("sl_usd") or _env_float("SIM_BRACKET_SL_USD", 30.0))
        tp = float(br.get("tp_usd") or _env_float("SIM_BRACKET_TP_USD", 60.0))
        policy = str(br.get("fill_policy") or _env_str("SIM_FILL_POLICY", "worst")).lower()
        if policy not in {"worst", "best"}:
            policy = "worst"
        return BracketRule(sl_usd=sl, tp_usd=tp, fill_policy=policy)

    def _compute_bracket_prices(
        self,
        *,
        contract_id: str,
        entry_price: float,
        position_type: int,
        sl_usd: float,
        tp_usd: float,
        size: int,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        Convert USD -> ticks -> price offset and return (sl_price, tp_price).

        USD is treated as *per contract*. Total risk scales with size.
        """
        tick_size, tick_value = self._contract_specs(contract_id)
        if tick_value <= 0 or tick_size <= 0:
            return None, None

        # Convert USD per contract into ticks (round to nearest tick)
        def _usd_to_offset(usd: float) -> float:
            ticks = int(round(float(usd) / float(tick_value)))
            return float(ticks) * float(tick_size)

        sl_off = _usd_to_offset(sl_usd) if sl_usd and sl_usd > 0 else 0.0
        tp_off = _usd_to_offset(tp_usd) if tp_usd and tp_usd > 0 else 0.0

        if sl_off <= 0:
            sl_price = None
        else:
            if position_type == POSITION_TYPE_LONG:
                sl_price = _round_to_tick(entry_price - sl_off, tick_size)
            else:
                sl_price = _round_to_tick(entry_price + sl_off, tick_size)

        if tp_off <= 0:
            tp_price = None
        else:
            if position_type == POSITION_TYPE_LONG:
                tp_price = _round_to_tick(entry_price + tp_off, tick_size)
            else:
                tp_price = _round_to_tick(entry_price - tp_off, tick_size)

        return sl_price, tp_price

    def _pnl_usd(self, contract_id: str, position_type: int, entry: float, exit: float, size: int) -> float:
        tick_size, tick_value = self._contract_specs(contract_id)
        value_per_point = tick_value / tick_size if tick_size else 0.0
        if value_per_point == 0:
            return 0.0
        if position_type == POSITION_TYPE_LONG:
            return (exit - entry) * value_per_point * int(size)
        return (entry - exit) * value_per_point * int(size)

    # ---------------- state accessors ----------------

    def _get_account_row(self, account_id: int) -> Optional[Dict[str, Any]]:
        for a in self.state.get("accounts") or []:
            if int(a.get("id")) == int(account_id):
                return a
        return None

    def _get_open_position(self, account_id: int, contract_id: str) -> Optional[Dict[str, Any]]:
        for p in self.state.get("positions") or []:
            if int(p.get("accountId")) == int(account_id) and p.get("contractId") == contract_id and not p.get("closedTimestamp"):
                return p
        return None

    def _open_positions_for_account(self, account_id: int) -> List[Dict[str, Any]]:
        return [
            p
            for p in (self.state.get("positions") or [])
            if int(p.get("accountId")) == int(account_id) and not p.get("closedTimestamp")
        ]

    def _orders_for_account(self, account_id: int) -> List[Dict[str, Any]]:
        return [o for o in (self.state.get("orders") or []) if int(o.get("accountId")) == int(account_id)]

    def _trades_for_account(self, account_id: int) -> List[Dict[str, Any]]:
        return [t for t in (self.state.get("trades") or []) if int(t.get("accountId")) == int(account_id)]

    def _order_by_id(self, order_id: int) -> Optional[Dict[str, Any]]:
        for o in self.state.get("orders") or []:
            if int(o.get("id")) == int(order_id):
                return o
        return None

    # ---------------- public "API" entry ----------------

    def handle(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main dispatcher used by api.py router in sim mode.

        path examples:
          /api/Order/place
          /api/Position/searchOpen
        """
        path = str(path or "").strip()
        payload = payload or {}

        try:
            if path == "/api/Auth/loginKey":
                return _wrap_success({"token": "SIM_TOKEN"})
            if path == "/api/Auth/validate":
                return _wrap_success({"newToken": "SIM_TOKEN"})

            if path == "/api/Account/search":
                return self._account_search(payload)

            if path == "/api/Contract/available":
                return self._contract_available(payload)
            if path == "/api/Contract/search":
                return self._contract_search(payload)
            if path == "/api/Contract/searchById":
                return self._contract_search_by_id(payload)

            if path == "/api/Order/place":
                return self._order_place(payload)
            if path == "/api/Order/search":
                return self._order_search(payload)
            if path == "/api/Order/searchOpen":
                return self._order_search_open(payload)
            if path == "/api/Order/cancel":
                return self._order_cancel(payload)
            if path == "/api/Order/modify":
                return self._order_modify(payload)

            if path == "/api/Position/searchOpen":
                return self._position_search_open(payload)
            if path == "/api/Position/closeContract":
                return self._position_close_contract(payload)
            if path == "/api/Position/partialCloseContract":
                return self._position_partial_close(payload)

            if path == "/api/Trade/search":
                return self._trade_search(payload)

            if path == "/api/History/retrieveBars":
                return self._history_retrieve_bars(payload)

            return _wrap_error(f"SimBroker: unsupported path {path}", code=404)
        except Exception as exc:
            log.exception("SimBroker error handling %s payload=%s", path, payload)
            return _wrap_error(str(exc), code=500)

    # ---------------- endpoints ----------------

    def _account_search(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.sync_accounts()
        only_active = bool(payload.get("onlyActiveAccounts", True))
        accounts = []
        for a in self.state.get("accounts") or []:
            if only_active and not a.get("canTrade", True):
                continue
            accounts.append(
                {
                    "id": int(a.get("id")),
                    "name": a.get("name"),
                    "balance": float(a.get("balance") or 0),
                    "canTrade": bool(a.get("canTrade", True)),
                    "isVisible": bool(a.get("isVisible", True)),
                }
            )
        return _wrap_success({"accounts": accounts})

    def _contract_available(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        live = bool(payload.get("live", True))
        # For sim mode, live flag doesn't matter; return a minimal contract set.
        contracts = []

        # Include at least the MES override contract and any contracts seen in state
        seen = set()
        for p in self.state.get("positions") or []:
            if p.get("contractId"):
                seen.add(p["contractId"])
        for o in self.state.get("orders") or []:
            if o.get("contractId"):
                seen.add(o["contractId"])

        if not seen:
            seen.add(os.getenv("OVERRIDE_CONTRACT_ID", "CON.F.US.MES.H26"))

        for cid in sorted(seen):
            root = _root_symbol_from_contract_id(cid)
            tick_size, tick_value = self._contract_specs(cid)
            symbol_id = _symbol_id_from_contract_id(cid) or f"F.US.{root}"
            contracts.append(
                {
                    "id": cid,
                    "name": f"{root}{cid.split('.')[-1]}",
                    "description": f"Simulated {root} contract ({'live' if live else 'sim'})",
                    "tickSize": float(tick_size),
                    "tickValue": float(tick_value),
                    "activeContract": True,
                    "symbolId": symbol_id,
                }
            )

        return _wrap_success({"contracts": contracts})

    def _contract_search(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # Minimal: delegate to available and optionally filter by symbolId
        symbol_id = payload.get("symbolId")
        resp = self._contract_available({"live": payload.get("live", True)})
        contracts = resp.get("contracts", []) or []
        if symbol_id:
            contracts = [c for c in contracts if c.get("symbolId") == symbol_id]
        return _wrap_success({"contracts": contracts})

    def _contract_search_by_id(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        cid = payload.get("contractId") or payload.get("id")
        if not cid:
            return _wrap_error("contractId required", code=400)
        root = _root_symbol_from_contract_id(str(cid))
        tick_size, tick_value = self._contract_specs(str(cid))
        symbol_id = _symbol_id_from_contract_id(str(cid)) or f"F.US.{root}"
        return _wrap_success(
            {
                "contract": {
                    "id": str(cid),
                    "name": f"{root}{str(cid).split('.')[-1]}",
                    "description": f"Simulated {root} contract",
                    "tickSize": float(tick_size),
                    "tickValue": float(tick_value),
                    "activeContract": True,
                    "symbolId": symbol_id,
                }
            }
        )

    def _order_place(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = int(payload.get("accountId"))
        contract_id = str(payload.get("contractId"))
        otype = int(payload.get("type"))
        side = int(payload.get("side"))
        size = int(payload.get("size") or 0)
        if size <= 0:
            return _wrap_error("size must be > 0", code=400)

        self.sync_accounts()
        acct = self._get_account_row(account_id)
        if not acct:
            return _wrap_error(f"Unknown accountId {account_id}", code=404)
        if not acct.get("canTrade", True):
            return _wrap_error(f"Account {account_id} cannot trade", code=403)

        now_price, bar_ts, _tf = self._latest_price(contract_id)
        if now_price is None or bar_ts is None:
            return _wrap_error("No market data available for fills", code=503)

        order_id = self._next_id("nextOrderId")
        symbol_id = _symbol_id_from_contract_id(contract_id)

        order = {
            "id": int(order_id),
            "accountId": int(account_id),
            "contractId": contract_id,
            "symbolId": symbol_id,
            "creationTimestamp": bar_ts,
            "updateTimestamp": bar_ts,
            "status": ORDER_STATUS_OPEN,
            "type": int(otype),
            "side": int(side),
            "size": int(size),
            "limitPrice": _safe_float(payload.get("limitPrice")),
            "stopPrice": _safe_float(payload.get("stopPrice")),
            "filledPrice": None,
            "fillVolume": 0,
            "customTag": payload.get("customTag"),
        }

        # Minimal viable: market fills immediately at latest close.
        if otype == ORDER_TYPE_MARKET:
            fill_price = float(now_price)
            order["status"] = ORDER_STATUS_FILLED
            order["filledPrice"] = fill_price
            order["fillVolume"] = int(size)
            order["updateTimestamp"] = bar_ts

            self.state["orders"].append(order)

            # Create / adjust position
            self._apply_fill_to_position(
                account_id=account_id,
                contract_id=contract_id,
                side=side,
                size=size,
                price=fill_price,
                ts=bar_ts,
                entry_order_id=order_id,
            )

            self._save_state()

            # ProjectX response shape: {orderId, success, errorCode, errorMessage}
            # Include fillPrice for extra compatibility with existing bot code.
            return _wrap_success({"orderId": int(order_id), "fillPrice": fill_price})

        # Optional: support limit/stop as OPEN orders (not filled immediately)
        if otype in (ORDER_TYPE_LIMIT, ORDER_TYPE_STOP):
            self.state["orders"].append(order)
            self._save_state()
            return _wrap_success({"orderId": int(order_id)})

        # Unsupported types
        order["status"] = ORDER_STATUS_REJECTED
        self.state["orders"].append(order)
        self._save_state()
        return _wrap_error(f"Unsupported order type {otype}", code=400)

    def _order_search(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = int(payload.get("accountId"))
        start_ts = _parse_ts(payload.get("startTimestamp"))
        end_ts = _parse_ts(payload.get("endTimestamp")) if payload.get("endTimestamp") else None
        if not start_ts:
            return _wrap_error("startTimestamp required", code=400)

        orders = []
        for o in self._orders_for_account(account_id):
            cts = _parse_ts(o.get("creationTimestamp"))
            if not cts:
                continue
            if cts < start_ts:
                continue
            if end_ts and cts > end_ts:
                continue
            orders.append(dict(o))

        # match doc examples (descending newest first)
        orders.sort(key=lambda x: _parse_ts(x.get("creationTimestamp")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return _wrap_success({"orders": orders})

    def _order_search_open(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = int(payload.get("accountId"))
        orders = [dict(o) for o in self._orders_for_account(account_id) if int(o.get("status")) == ORDER_STATUS_OPEN]
        orders.sort(key=lambda x: _parse_ts(x.get("creationTimestamp")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return _wrap_success({"orders": orders})

    def _order_cancel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = int(payload.get("accountId"))
        order_id = int(payload.get("orderId"))
        o = self._order_by_id(order_id)
        if not o or int(o.get("accountId")) != int(account_id):
            return _wrap_error("order not found", code=404)
        if int(o.get("status")) != ORDER_STATUS_OPEN:
            return _wrap_success({})  # cancelling non-open is idempotent success

        now_iso = _utc_now_iso()
        o["status"] = ORDER_STATUS_CANCELLED
        o["updateTimestamp"] = now_iso

        # If it's a bracket child, remove it from bracket mapping
        self._unlink_bracket_child_if_needed(account_id, o)

        self._save_state()
        return _wrap_success({})

    def _order_modify(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = int(payload.get("accountId"))
        order_id = int(payload.get("orderId"))
        o = self._order_by_id(order_id)
        if not o or int(o.get("accountId")) != int(account_id):
            return _wrap_error("order not found", code=404)
        if int(o.get("status")) != ORDER_STATUS_OPEN:
            return _wrap_error("only open orders can be modified", code=400)

        if payload.get("size") is not None:
            o["size"] = int(payload.get("size") or o.get("size") or 0)
        if payload.get("limitPrice") is not None:
            o["limitPrice"] = _safe_float(payload.get("limitPrice"))
        if payload.get("stopPrice") is not None:
            o["stopPrice"] = _safe_float(payload.get("stopPrice"))

        o["updateTimestamp"] = _utc_now_iso()
        self._save_state()
        return _wrap_success({})

    def _position_search_open(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = int(payload.get("accountId"))
        positions = []
        for p in self._open_positions_for_account(account_id):
            positions.append(
                {
                    "id": int(p.get("id")),
                    "accountId": int(p.get("accountId")),
                    "contractId": p.get("contractId"),
                    "creationTimestamp": p.get("creationTimestamp"),
                    "type": int(p.get("type")),
                    "size": int(p.get("size")),
                    "averagePrice": float(p.get("averagePrice")),
                }
            )
        return _wrap_success({"positions": positions})

    def _position_close_contract(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = int(payload.get("accountId"))
        contract_id = str(payload.get("contractId"))

        pos = self._get_open_position(account_id, contract_id)
        if not pos:
            return _wrap_success({})

        now_price, bar_ts, _tf = self._latest_price(contract_id)
        if now_price is None or bar_ts is None:
            return _wrap_error("No market data to close position", code=503)

        self._close_position(
            account_id=account_id,
            contract_id=contract_id,
            exit_price=float(now_price),
            exit_ts=bar_ts,
            reason="manual_close",
        )
        self._save_state()
        return _wrap_success({})

    def _position_partial_close(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = int(payload.get("accountId"))
        contract_id = str(payload.get("contractId"))
        close_size = int(payload.get("size") or 0)
        if close_size <= 0:
            return _wrap_error("size must be > 0", code=400)

        pos = self._get_open_position(account_id, contract_id)
        if not pos:
            return _wrap_success({})

        cur_size = int(pos.get("size") or 0)
        if close_size > cur_size:
            close_size = cur_size

        now_price, bar_ts, _tf = self._latest_price(contract_id)
        if now_price is None or bar_ts is None:
            return _wrap_error("No market data to close position", code=503)

        # Realize pnl for the closed portion via an "exit trade"
        self._realize_partial_close(
            account_id=account_id,
            contract_id=contract_id,
            close_size=close_size,
            exit_price=float(now_price),
            exit_ts=bar_ts,
            reason="partial_close",
        )

        # If position is fully closed, it is handled inside _realize_partial_close.
        # Otherwise update bracket sizes.
        self._save_state()
        return _wrap_success({})

    def _trade_search(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        account_id = int(payload.get("accountId"))
        start_ts = _parse_ts(payload.get("startTimestamp"))
        end_ts = _parse_ts(payload.get("endTimestamp")) if payload.get("endTimestamp") else None
        if not start_ts:
            return _wrap_error("startTimestamp required", code=400)

        trades = []
        for t in self._trades_for_account(account_id):
            cts = _parse_ts(t.get("creationTimestamp"))
            if not cts:
                continue
            if cts < start_ts:
                continue
            if end_ts and cts > end_ts:
                continue
            trades.append(dict(t))

        # match doc example order (descending newest first)
        trades.sort(key=lambda x: _parse_ts(x.get("creationTimestamp")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return _wrap_success({"trades": trades})

    def _history_retrieve_bars(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        contract_id = str(payload.get("contractId"))
        start = _parse_ts(payload.get("startTime"))
        end = _parse_ts(payload.get("endTime"))
        unit = int(payload.get("unit") or 2)  # 2=Minute by docs
        unit_n = int(payload.get("unitNumber") or 1)
        limit = int(payload.get("limit") or 2000)

        if not contract_id or not start or not end:
            return _wrap_error("contractId, startTime, endTime required", code=400)

        # map unit/unitNumber -> timeframe filters (best-effort)
        tf_filters: List[str] = []
        if unit == 2:  # minute
            tf_filters = [f"{unit_n}m", str(unit_n)]
        elif unit == 4:  # day
            tf_filters = ["1D", "1d", "D", "1440"]
        else:
            # fallback to minutes
            tf_filters = [f"{unit_n}m", str(unit_n)]

        bars = self._bars_since(contract_id, start_ts_exclusive=None, end_ts_inclusive=_iso(end), limit=limit)
        # server returns descending newest first; and "t" field name
        out = []
        for b in bars:
            bdt = _parse_ts(b.ts)
            if not bdt:
                continue
            if bdt < start or bdt > end:
                continue
            out.append({"t": b.ts, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": int(b.v)})

        out.sort(key=lambda x: _parse_ts(x["t"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        if limit:
            out = out[: int(limit)]
        return _wrap_success({"bars": out})

    # ---------------- simulation update ----------------

    def sim_update(self, account_id: int, contract_id: Optional[str] = None, now_ts_iso: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Advance simulation time and trigger bracket exits.

        Returns a list of closure events:
          {"accountId":..., "contractId":..., "exitTimestamp":..., "reason":"tp|sl|manual_close|partial_close", ...}

        This does NOT write to Supabase. api.py is responsible for logging trade_results.
        """
        with self._lock:
            events: List[Dict[str, Any]] = []
            now_ts = _parse_ts(now_ts_iso) if now_ts_iso else None
            now_iso = _iso(now_ts) if now_ts else None

            positions = self._open_positions_for_account(int(account_id))
            if contract_id:
                positions = [p for p in positions if p.get("contractId") == contract_id]

            for pos in positions:
                cid = str(pos.get("contractId"))
                key = f"{int(account_id)}|{cid}"
                last_ts = self.state.get("last_processed_bar_ts", {}).get(key)

                # Pull new bars since last_ts. If now_iso is set, don't process beyond it.
                bars = self._bars_since(cid, start_ts_exclusive=last_ts, end_ts_inclusive=now_iso, limit=5000)
                if not bars:
                    continue

                bracket = (self.state.get("brackets") or {}).get(key) or {}
                sl_price = bracket.get("sl_price")
                tp_price = bracket.get("tp_price")
                sl_order_id = bracket.get("sl_order_id")
                tp_order_id = bracket.get("tp_order_id")

                position_type = int(pos.get("type"))
                entry_price = float(pos.get("averagePrice"))
                size = int(pos.get("size"))

                br_rule = self._bracket_for_account(int(account_id))
                policy = br_rule.fill_policy

                closed = False
                for b in bars:
                    high = float(b.h)
                    low = float(b.l)

                    tp_hit = False
                    sl_hit = False

                    if tp_price is not None:
                        if position_type == POSITION_TYPE_LONG:
                            tp_hit = high >= float(tp_price)
                        else:
                            tp_hit = low <= float(tp_price)

                    if sl_price is not None:
                        if position_type == POSITION_TYPE_LONG:
                            sl_hit = low <= float(sl_price)
                        else:
                            sl_hit = high >= float(sl_price)

                    if not tp_hit and not sl_hit:
                        # advance cursor
                        self.state["last_processed_bar_ts"][key] = b.ts
                        continue

                    # Both hit in same bar -> choose per policy
                    trigger = None
                    if tp_hit and sl_hit:
                        trigger = "sl" if policy == "worst" else "tp"
                    elif tp_hit:
                        trigger = "tp"
                    else:
                        trigger = "sl"

                    exit_price = float(tp_price) if trigger == "tp" else float(sl_price)

                    self._close_position(
                        account_id=int(account_id),
                        contract_id=cid,
                        exit_price=exit_price,
                        exit_ts=b.ts,
                        reason=trigger,
                        triggered_order_id=int(tp_order_id) if trigger == "tp" and tp_order_id else int(sl_order_id) if trigger == "sl" and sl_order_id else None,
                    )

                    self.state["last_processed_bar_ts"][key] = b.ts
                    events.append(
                        {
                            "accountId": int(account_id),
                            "contractId": cid,
                            "exitTimestamp": b.ts,
                            "reason": trigger,
                            "entryPrice": entry_price,
                            "exitPrice": exit_price,
                            "size": size,
                        }
                    )
                    closed = True
                    break

                # If not closed, cursor already advanced inside loop; ensure cursor at last bar.
                if not closed and bars:
                    self.state["last_processed_bar_ts"][key] = bars[-1].ts

            if events:
                self._save_state()
            return events

    # ---------------- internal mechanics ----------------

    def _apply_fill_to_position(
        self,
        *,
        account_id: int,
        contract_id: str,
        side: int,
        size: int,
        price: float,
        ts: str,
        entry_order_id: int,
    ) -> None:
        """
        Apply a filled entry order to positions and create entry trade.
        Also attaches/updates broker-side bracket.
        """
        pos = self._get_open_position(account_id, contract_id)

        # Determine direction of the fill
        fill_dir = POSITION_TYPE_LONG if int(side) == ORDER_SIDE_BID else POSITION_TYPE_SHORT

        if pos is None:
            pos_id = self._next_id("nextPositionId")
            pos = {
                "id": int(pos_id),
                "accountId": int(account_id),
                "contractId": contract_id,
                "creationTimestamp": ts,
                "type": int(fill_dir),
                "size": int(size),
                "averagePrice": float(price),
            }
            self.state["positions"].append(pos)
        else:
            # If same direction, add to position and recompute weighted avg
            if int(pos.get("type")) == int(fill_dir):
                old_size = int(pos.get("size") or 0)
                new_size = old_size + int(size)
                if new_size <= 0:
                    new_size = int(size)
                old_avg = float(pos.get("averagePrice") or price)
                new_avg = ((old_avg * old_size) + (price * size)) / float(new_size)
                pos["size"] = int(new_size)
                pos["averagePrice"] = float(new_avg)
            else:
                # Opposite direction reduces or flips. Minimal approach: close existing then open new for remainder.
                old_size = int(pos.get("size") or 0)
                close_size = min(old_size, int(size))
                # Realize PnL for the closed portion at this fill price.
                self._realize_partial_close(
                    account_id=account_id,
                    contract_id=contract_id,
                    close_size=close_size,
                    exit_price=float(price),
                    exit_ts=ts,
                    reason="netting_close",
                )
                remainder = int(size) - int(close_size)
                if remainder > 0:
                    # Open new position in fill direction for remainder
                    pos2 = self._get_open_position(account_id, contract_id)
                    if pos2 is None:
                        pos_id = self._next_id("nextPositionId")
                        pos2 = {
                            "id": int(pos_id),
                            "accountId": int(account_id),
                            "contractId": contract_id,
                            "creationTimestamp": ts,
                            "type": int(fill_dir),
                            "size": int(remainder),
                            "averagePrice": float(price),
                        }
                        self.state["positions"].append(pos2)
                    else:
                        pos2["type"] = int(fill_dir)
                        pos2["size"] = int(remainder)
                        pos2["averagePrice"] = float(price)

        # Entry trade record (half-turn PnL is null per ProjectX docs)
        trade_id = self._next_id("nextTradeId")
        trade = {
            "id": int(trade_id),
            "accountId": int(account_id),
            "contractId": contract_id,
            "creationTimestamp": ts,
            "price": float(price),
            "profitAndLoss": None,
            "fees": 0.0,
            "side": int(side),
            "size": int(size),
            "voided": False,
            "orderId": int(entry_order_id),
        }
        self.state["trades"].append(trade)

        # Attach broker-side bracket for this (possibly new) position
        pos_now = self._get_open_position(account_id, contract_id)
        if pos_now:
            self._ensure_bracket_for_position(account_id, contract_id, pos_now, parent_order_id=int(entry_order_id), ts=ts)

        # Initialize last_processed_bar_ts cursor for deterministic replay
        key = f"{int(account_id)}|{contract_id}"
        self.state.setdefault("last_processed_bar_ts", {})
        if key not in self.state["last_processed_bar_ts"]:
            self.state["last_processed_bar_ts"][key] = ts

    def _ensure_bracket_for_position(self, account_id: int, contract_id: str, pos: Dict[str, Any], parent_order_id: int, ts: str) -> None:
        key = f"{int(account_id)}|{contract_id}"
        br_rule = self._bracket_for_account(account_id)

        sl_price, tp_price = self._compute_bracket_prices(
            contract_id=contract_id,
            entry_price=float(pos.get("averagePrice")),
            position_type=int(pos.get("type")),
            sl_usd=float(br_rule.sl_usd),
            tp_usd=float(br_rule.tp_usd),
            size=int(pos.get("size")),
        )

        brackets = self.state.setdefault("brackets", {})
        existing = brackets.get(key) or {}

        # Create synthetic child orders if missing
        if not existing.get("sl_order_id") and sl_price is not None:
            sl_order_id = self._next_id("nextOrderId")
            sl_order = {
                "id": int(sl_order_id),
                "accountId": int(account_id),
                "contractId": contract_id,
                "symbolId": _symbol_id_from_contract_id(contract_id),
                "creationTimestamp": ts,
                "updateTimestamp": ts,
                "status": ORDER_STATUS_OPEN,
                "type": ORDER_TYPE_STOP,
                "side": ORDER_SIDE_ASK if int(pos.get("type")) == POSITION_TYPE_LONG else ORDER_SIDE_BID,
                "size": int(pos.get("size")),
                "limitPrice": None,
                "stopPrice": float(sl_price),
                "filledPrice": None,
                "fillVolume": 0,
                "customTag": f"SIM_BRACKET_SL_PARENT_{parent_order_id}",
            }
            self.state["orders"].append(sl_order)
            existing["sl_order_id"] = int(sl_order_id)

        if not existing.get("tp_order_id") and tp_price is not None:
            tp_order_id = self._next_id("nextOrderId")
            tp_order = {
                "id": int(tp_order_id),
                "accountId": int(account_id),
                "contractId": contract_id,
                "symbolId": _symbol_id_from_contract_id(contract_id),
                "creationTimestamp": ts,
                "updateTimestamp": ts,
                "status": ORDER_STATUS_OPEN,
                "type": ORDER_TYPE_LIMIT,
                "side": ORDER_SIDE_ASK if int(pos.get("type")) == POSITION_TYPE_LONG else ORDER_SIDE_BID,
                "size": int(pos.get("size")),
                "limitPrice": float(tp_price),
                "stopPrice": None,
                "filledPrice": None,
                "fillVolume": 0,
                "customTag": f"SIM_BRACKET_TP_PARENT_{parent_order_id}",
            }
            self.state["orders"].append(tp_order)
            existing["tp_order_id"] = int(tp_order_id)

        # Always update bracket prices + sizes (in case position size/avg changed)
        existing["parent_order_id"] = int(parent_order_id)
        existing["position_id"] = int(pos.get("id"))
        existing["sl_price"] = float(sl_price) if sl_price is not None else None
        existing["tp_price"] = float(tp_price) if tp_price is not None else None

        # Update child order sizes + prices
        if existing.get("sl_order_id"):
            o = self._order_by_id(int(existing["sl_order_id"]))
            if o and int(o.get("status")) == ORDER_STATUS_OPEN:
                o["size"] = int(pos.get("size"))
                o["stopPrice"] = float(sl_price) if sl_price is not None else o.get("stopPrice")
                o["updateTimestamp"] = ts
        if existing.get("tp_order_id"):
            o = self._order_by_id(int(existing["tp_order_id"]))
            if o and int(o.get("status")) == ORDER_STATUS_OPEN:
                o["size"] = int(pos.get("size"))
                o["limitPrice"] = float(tp_price) if tp_price is not None else o.get("limitPrice")
                o["updateTimestamp"] = ts

        brackets[key] = existing

    def _unlink_bracket_child_if_needed(self, account_id: int, order_row: Dict[str, Any]) -> None:
        """If a cancelled order is a bracket child, clear it from bracket mapping."""
        contract_id = order_row.get("contractId")
        if not contract_id:
            return
        key = f"{int(account_id)}|{contract_id}"
        br = (self.state.get("brackets") or {}).get(key)
        if not br:
            return
        oid = int(order_row.get("id"))
        if br.get("sl_order_id") == oid:
            br["sl_order_id"] = None
            br["sl_price"] = None
        if br.get("tp_order_id") == oid:
            br["tp_order_id"] = None
            br["tp_price"] = None
        self.state["brackets"][key] = br

    def _close_position(
        self,
        *,
        account_id: int,
        contract_id: str,
        exit_price: float,
        exit_ts: str,
        reason: str,
        triggered_order_id: Optional[int] = None,
    ) -> None:
        """Close full open position and mark bracket orders accordingly."""
        pos = self._get_open_position(account_id, contract_id)
        if not pos:
            return

        entry_price = float(pos.get("averagePrice"))
        size = int(pos.get("size") or 0)
        position_type = int(pos.get("type"))

        # Close position
        pos["closedTimestamp"] = exit_ts
        pos["closedPrice"] = float(exit_price)

        # Mark bracket child orders
        key = f"{int(account_id)}|{contract_id}"
        br = (self.state.get("brackets") or {}).get(key) or {}
        sl_order_id = br.get("sl_order_id")
        tp_order_id = br.get("tp_order_id")

        # Determine which order is the exit order
        exit_order_id = triggered_order_id
        if exit_order_id is None:
            if reason == "tp" and tp_order_id:
                exit_order_id = int(tp_order_id)
            elif reason == "sl" and sl_order_id:
                exit_order_id = int(sl_order_id)

        if sl_order_id:
            o = self._order_by_id(int(sl_order_id))
            if o and int(o.get("status")) == ORDER_STATUS_OPEN:
                if int(exit_order_id or -1) == int(sl_order_id):
                    o["status"] = ORDER_STATUS_FILLED
                    o["filledPrice"] = float(exit_price)
                    o["fillVolume"] = int(size)
                else:
                    o["status"] = ORDER_STATUS_CANCELLED
                o["updateTimestamp"] = exit_ts

        if tp_order_id:
            o = self._order_by_id(int(tp_order_id))
            if o and int(o.get("status")) == ORDER_STATUS_OPEN:
                if int(exit_order_id or -1) == int(tp_order_id):
                    o["status"] = ORDER_STATUS_FILLED
                    o["filledPrice"] = float(exit_price)
                    o["fillVolume"] = int(size)
                else:
                    o["status"] = ORDER_STATUS_CANCELLED
                o["updateTimestamp"] = exit_ts

        # Create exit trade record (full-turn has pnl per ProjectX docs)
        pnl = self._pnl_usd(contract_id, position_type, entry_price, float(exit_price), size)
        exit_side = ORDER_SIDE_ASK if position_type == POSITION_TYPE_LONG else ORDER_SIDE_BID

        trade_id = self._next_id("nextTradeId")
        trade = {
            "id": int(trade_id),
            "accountId": int(account_id),
            "contractId": contract_id,
            "creationTimestamp": exit_ts,
            "price": float(exit_price),
            "profitAndLoss": float(pnl),
            "fees": 0.0,
            "side": int(exit_side),
            "size": int(size),
            "voided": False,
            "orderId": int(exit_order_id) if exit_order_id is not None else None,
        }
        self.state["trades"].append(trade)

        # Update account balance
        acct = self._get_account_row(account_id)
        if acct is not None:
            acct["balance"] = float(acct.get("balance") or 0) + float(pnl)

        # deactivate bracket mapping (kept for audit but won't be used)
        if "brackets" in self.state and key in self.state["brackets"]:
            self.state["brackets"][key]["active"] = False

    def _realize_partial_close(
        self,
        *,
        account_id: int,
        contract_id: str,
        close_size: int,
        exit_price: float,
        exit_ts: str,
        reason: str,
    ) -> None:
        pos = self._get_open_position(account_id, contract_id)
        if not pos:
            return

        close_size = int(close_size)
        if close_size <= 0:
            return

        entry_price = float(pos.get("averagePrice"))
        position_type = int(pos.get("type"))
        cur_size = int(pos.get("size") or 0)
        close_size = min(close_size, cur_size)

        pnl = self._pnl_usd(contract_id, position_type, entry_price, float(exit_price), close_size)
        exit_side = ORDER_SIDE_ASK if position_type == POSITION_TYPE_LONG else ORDER_SIDE_BID

        # Create a synthetic exit order id (or reuse an existing open bracket order if any)
        key = f"{int(account_id)}|{contract_id}"
        br = (self.state.get("brackets") or {}).get(key) or {}
        exit_order_id = br.get("tp_order_id") or br.get("sl_order_id") or self._next_id("nextOrderId")

        trade_id = self._next_id("nextTradeId")
        trade = {
            "id": int(trade_id),
            "accountId": int(account_id),
            "contractId": contract_id,
            "creationTimestamp": exit_ts,
            "price": float(exit_price),
            "profitAndLoss": float(pnl),
            "fees": 0.0,
            "side": int(exit_side),
            "size": int(close_size),
            "voided": False,
            "orderId": int(exit_order_id),
        }
        self.state["trades"].append(trade)

        # Update account balance
        acct = self._get_account_row(account_id)
        if acct is not None:
            acct["balance"] = float(acct.get("balance") or 0) + float(pnl)

        # Adjust position size
        remaining = int(cur_size) - int(close_size)
        if remaining <= 0:
            # full close
            self._close_position(
                account_id=account_id,
                contract_id=contract_id,
                exit_price=float(exit_price),
                exit_ts=exit_ts,
                reason=reason,
                triggered_order_id=int(exit_order_id),
            )
        else:
            pos["size"] = int(remaining)
            # update bracket children sizes to match remaining
            if br.get("sl_order_id"):
                o = self._order_by_id(int(br["sl_order_id"]))
                if o and int(o.get("status")) == ORDER_STATUS_OPEN:
                    o["size"] = int(remaining)
                    o["updateTimestamp"] = exit_ts
            if br.get("tp_order_id"):
                o = self._order_by_id(int(br["tp_order_id"]))
                if o and int(o.get("status")) == ORDER_STATUS_OPEN:
                    o["size"] = int(remaining)
                    o["updateTimestamp"] = exit_ts

