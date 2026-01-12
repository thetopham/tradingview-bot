# config.py
import os
import json
from pathlib import Path
from dotenv import load_dotenv
from datetime import time as dtime
import pytz



def _load_accounts_from_sim_file(path: str) -> dict:
    """Load accounts mapping from SIM_ACCOUNTS_FILE.

    Accepts either:
      A) {"sim001": {"id": 900001, ...}, "sim002": {"id": 900002, ...}}
      B) [{"name":"sim001","id":900001,...}, {"name":"sim002","id":900002,...}]

    Returns mapping: {name_lower: id_int}
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}

    out = {}
    if isinstance(data, dict):
        for name, cfg in data.items():
            if not name:
                continue
            if isinstance(cfg, dict) and cfg.get("id") is not None:
                try:
                    out[str(name).lower()] = int(cfg.get("id"))
                except Exception:
                    continue
    elif isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip().lower()
            if not name:
                continue
            if row.get("id") is None:
                continue
            try:
                out[name] = int(row.get("id"))
            except Exception:
                continue
    return out


def load_config():
    load_dotenv()

    def _env_bool(key: str, default: bool = False) -> bool:
        raw = os.getenv(key)
        if raw is None:
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}

    def _env_float_opt(key: str, default=None):
        raw = os.getenv(key)
        if raw is None:
            return default
        raw = str(raw).strip()
        if raw == "":
            return default
        try:
            return float(raw)
        except Exception:
            return default
    config = {
        'TV_PORT': int(os.getenv("TV_PORT", 5000)),
        'PX_BASE': os.getenv("PROJECTX_BASE_URL"),
        'USER_NAME': os.getenv("PROJECTX_USERNAME"),
        'API_KEY': os.getenv("PROJECTX_API_KEY"),
        'WEBHOOK_SECRET': os.getenv("WEBHOOK_SECRET"),
        'DASHBOARD_PASSWORD': os.getenv("DASHBOARD_PASSWORD"),
        'N8N_AI_URL': os.getenv("N8N_AI_URL"),
        'N8N_AI_URL2': os.getenv("N8N_AI_URL2"),
        'N8N_CHART_FETCH_URL': os.getenv("N8N_CHART_FETCH_URL"),
        'N8N_5MCHART_FETCH_URL': os.getenv("N8N_5MCHART_FETCH_URL"),
        'N8N_15MCHART_FETCH_URL': os.getenv("N8N_15MCHART_FETCH_URL"),
        'N8N_30MCHART_FETCH_URL': os.getenv("N8N_30MCHART_FETCH_URL"),
        'N8N_OVERSEER_URL_TEST1': os.getenv("N8N_OVERSEER_URL_TEST1"),
        'N8N_OVERSEER_URL_TEST2': os.getenv("N8N_OVERSEER_URL_TEST2"),
        'N8N_OVERSEER_URL_TEST3': os.getenv("N8N_OVERSEER_URL_TEST3"),
        'N8N_OVERSEER_URL_TEST4': os.getenv("N8N_OVERSEER_URL_TEST4"),
        'N8N_OVERSEER_URL_TEST5': os.getenv("N8N_OVERSEER_URL_TEST5"),
        'N8N_OVERSEER_URL_TEST6': os.getenv("N8N_OVERSEER_URL_TEST6"),
        'SUPABASE_URL': os.getenv("SUPABASE_URL"),
        'SUPABASE_KEY': os.getenv("SUPABASE_KEY"),
        'WEBHOOK': os.getenv("WEBHOOK"),
        # Risk params
        'DAILY_PROFIT_TARGET': float(os.getenv("DAILY_PROFIT_TARGET", 99999.0)),
        'MAX_DAILY_LOSS': float(os.getenv("MAX_DAILY_LOSS", -250.0)),
        'MAX_CONSECUTIVE_LOSSES': int(os.getenv("MAX_CONSECUTIVE_LOSSES", 99999)),

        # -----------------------------------------------------------------
        # Regime filter (blocks trades in chop / high-vol; allows LV trends)
        # -----------------------------------------------------------------
        # Enable/disable the regime gate
        'REGIME_FILTER_ENABLED': _env_bool("REGIME_FILTER_ENABLED", True),
        # If enabled and we cannot classify (no data / error), block trades
        'REGIME_FAIL_CLOSED': _env_bool("REGIME_FAIL_CLOSED", True),

        # Supabase tables (defaults match the scanner/n8n conventions)
        'REGIME_TABLE_5M': os.getenv("REGIME_TABLE_5M", "tv_datafeed_5m"),
        'REGIME_TABLE_HTF': os.getenv("REGIME_TABLE_HTF", "tv_datafeed_30m"),
        'REGIME_TABLE_HTF_FALLBACK': os.getenv("REGIME_TABLE_HTF_FALLBACK", "tv_datafeed_15m"),

        # Lookbacks
        'REGIME_LOOKBACK_BARS': int(os.getenv("REGIME_LOOKBACK_BARS", 140)),
        'REGIME_ER_LOOKBACK': int(os.getenv("REGIME_ER_LOOKBACK", 12)),
        'REGIME_ATR_PCTL_LOOKBACK': int(os.getenv("REGIME_ATR_PCTL_LOOKBACK", 50)),

        # Thresholds (tuned for LV trend filtering)
        'REGIME_ER_MIN': float(os.getenv("REGIME_ER_MIN", 0.35)),
        'REGIME_ATR_PCTL_MAX': float(os.getenv("REGIME_ATR_PCTL_MAX", 0.25)),
        'REGIME_EMA_SPREAD_ATR_MIN': float(os.getenv("REGIME_EMA_SPREAD_ATR_MIN", 0.30)),
        'REGIME_SLOPE_ATR_MIN': float(os.getenv("REGIME_SLOPE_ATR_MIN", 0.50)),
        'REGIME_REQUIRE_HTF_ALIGN': _env_bool("REGIME_REQUIRE_HTF_ALIGN", True),

        # Optional absolute ATR guards (in price points). Leave blank to disable.
        'REGIME_ATR_MIN_POINTS': _env_float_opt("REGIME_ATR_MIN_POINTS", None),
        'REGIME_ATR_MAX_POINTS': _env_float_opt("REGIME_ATR_MAX_POINTS", None),
    }
    # Broker mode (live|sim)
    broker_mode = (os.getenv("BROKER_MODE") or "live").strip().lower()
    if broker_mode not in ("live", "sim"):
        broker_mode = "live"
    config["BROKER_MODE"] = broker_mode

    # Build account map
    accounts = {
        k[len("ACCOUNT_"):].lower(): int(v)
        for k, v in os.environ.items()
        if k.startswith("ACCOUNT_") and str(v).strip().lstrip("-").isdigit()
    }

    # If no ACCOUNT_* vars, allow sim-mode account generation
    if not accounts and broker_mode == "sim":
        # 1) SIM_ACCOUNTS_FILE (best for lots of accounts)
        sim_file = (os.getenv("SIM_ACCOUNTS_FILE") or "").strip()
        accounts.update(_load_accounts_from_sim_file(sim_file))

        # 2) SIM_ACCOUNTS list: "sim001,sim002,sim003"
        if not accounts:
            sim_list = (os.getenv("SIM_ACCOUNTS") or "").strip()
            if sim_list:
                start = int(float(os.getenv("SIM_ACCOUNT_ID_START", "900000")))
                names = [x.strip().lower() for x in sim_list.split(",") if x.strip()]
                accounts = {name: start + i for i, name in enumerate(names)}

        # 3) final fallback
        if not accounts:
            accounts = {"sim001": int(float(os.getenv("SIM_ACCOUNT_ID_START", "900000")))}

    if not accounts:
        raise RuntimeError("No accounts loaded from .env. Add ACCOUNT_<NAME>=<ID>, or set BROKER_MODE=sim with SIM_ACCOUNTS[_FILE].")

    config['ACCOUNTS'] = accounts
    config['DEFAULT_ACCOUNT'] = next(iter(config['ACCOUNTS']))
    config['OVERRIDE_CONTRACT_ID'] = os.getenv("OVERRIDE_CONTRACT_ID", "CON.F.US.MES.H26")
    config['STOP_LOSS_POINTS'] = float(os.getenv("STOP_LOSS_POINTS", 5.75))
    config['TP_POINTS'] = (
        [float(x) for x in os.getenv("TP_POINTS", "").split(",") if x.strip()]
        or [2.5, 5.0]
    )
    config['TICKS_PER_POINT'] = float(os.getenv("TICKS_PER_POINT", 4))
    mountain = pytz.timezone("America/Denver")

    # Trading hours are defined in Mountain Time (America/Denver)
    config['GET_FLAT_START'] = dtime(14, 5)  # 2:05pm MT
    config['GET_FLAT_END'] = dtime(16, 0)    # 4:00pm MT
    # Markets stay flat on Saturday and reopen Sunday at 4:00pm MT
    config['WEEKEND_MARKET_OPEN'] = dtime(16, 0)
    config['MT'] = mountain
    return config
