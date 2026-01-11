import os
from dotenv import load_dotenv
from datetime import time as dtime
import pytz

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
        'BROKER_MODE': os.getenv("BROKER_MODE", "live").strip().lower(),
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
        'SIM_BRACKET_SL_USD': float(os.getenv("SIM_BRACKET_SL_USD", 30)),
        'SIM_BRACKET_TP_USD': float(os.getenv("SIM_BRACKET_TP_USD", 60)),
        'SIM_TICK_SIZE': float(os.getenv("SIM_TICK_SIZE", 0.25)),
        'SIM_TICK_VALUE': float(os.getenv("SIM_TICK_VALUE", 1.25)),
        'SIM_FILL_POLICY': os.getenv("SIM_FILL_POLICY", "worst").strip().lower(),
        # Risk params
        'DAILY_PROFIT_TARGET': float(os.getenv("DAILY_PROFIT_TARGET", 99999.0)),
        'MAX_DAILY_LOSS': float(os.getenv("MAX_DAILY_LOSS", -250.0)),
        'MAX_CONSECUTIVE_LOSSES': int(os.getenv("MAX_CONSECUTIVE_LOSSES", 99999)),

        # -----------------------------------------------------------------
        # Regime filter (blocks trades in chop / high-vol; allows LV trends)
        # -----------------------------------------------------------------
        'REGIME_FILTER_ENABLED': _env_bool("REGIME_FILTER_ENABLED", True),
        'REGIME_FAIL_CLOSED': _env_bool("REGIME_FAIL_CLOSED", True),

        'REGIME_TABLE_5M': os.getenv("REGIME_TABLE_5M", "tv_datafeed_5m"),
        'REGIME_TABLE_HTF': os.getenv("REGIME_TABLE_HTF", "tv_datafeed_30m"),
        'REGIME_TABLE_HTF_FALLBACK': os.getenv("REGIME_TABLE_HTF_FALLBACK", "tv_datafeed_15m"),

        'REGIME_LOOKBACK_BARS': int(os.getenv("REGIME_LOOKBACK_BARS", 140)),
        'REGIME_ER_LOOKBACK': int(os.getenv("REGIME_ER_LOOKBACK", 12)),
        'REGIME_ATR_PCTL_LOOKBACK': int(os.getenv("REGIME_ATR_PCTL_LOOKBACK", 50)),

        'REGIME_ER_MIN': float(os.getenv("REGIME_ER_MIN", 0.35)),
        'REGIME_ATR_PCTL_MAX': float(os.getenv("REGIME_ATR_PCTL_MAX", 0.25)),
        'REGIME_EMA_SPREAD_ATR_MIN': float(os.getenv("REGIME_EMA_SPREAD_ATR_MIN", 0.30)),
        'REGIME_SLOPE_ATR_MIN': float(os.getenv("REGIME_SLOPE_ATR_MIN", 0.50)),
        'REGIME_REQUIRE_HTF_ALIGN': _env_bool("REGIME_REQUIRE_HTF_ALIGN", True),

        'REGIME_ATR_MIN_POINTS': _env_float_opt("REGIME_ATR_MIN_POINTS", None),
        'REGIME_ATR_MAX_POINTS': _env_float_opt("REGIME_ATR_MAX_POINTS", None),
    }

    config['ACCOUNTS'] = {
        k[len("ACCOUNT_"):].lower(): int(v)
        for k, v in os.environ.items() if k.startswith("ACCOUNT_")
    }
    if not config['ACCOUNTS']:
        raise RuntimeError("No accounts loaded from .env. Add ACCOUNT_<NAME>=<ID>.")
    config['DEFAULT_ACCOUNT'] = next(iter(config['ACCOUNTS']))
    config['OVERRIDE_CONTRACT_ID'] = os.getenv("OVERRIDE_CONTRACT_ID", "CON.F.US.MES.H26")
    config['STOP_LOSS_POINTS'] = float(os.getenv("STOP_LOSS_POINTS", 5.75))
    config['TP_POINTS'] = (
        [float(x) for x in os.getenv("TP_POINTS", "").split(",") if x.strip()]
        or [2.5, 5.0]
    )
    config['TICKS_PER_POINT'] = float(os.getenv("TICKS_PER_POINT", 4))

    mountain = pytz.timezone("America/Denver")
    config['GET_FLAT_START'] = dtime(14, 5)  # 2:05pm MT
    config['GET_FLAT_END'] = dtime(16, 0)    # 4:00pm MT
    config['WEEKEND_MARKET_OPEN'] = dtime(16, 0)
    config['MT'] = mountain
    return config
