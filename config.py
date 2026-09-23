# config.py
import os
from pathlib import Path
import sqlite3
from dotenv import load_dotenv
from datetime import time as dtime
import pytz

def load_config():
    load_dotenv()
    broker_mode = os.getenv("BROKER_MODE", "projectx").lower()
    if broker_mode not in {"projectx", "sim"}:
        raise ValueError("BROKER_MODE must be projectx or sim")
    config = {
        'BROKER_MODE': broker_mode,
        'SIM_BROKER_DB': os.getenv("SIM_BROKER_DB"),
        'SIM_MAX_BAR_LAG_SECONDS': int(os.getenv("SIM_MAX_BAR_LAG_SECONDS", "0")),
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
    }
    # Build account map
    if broker_mode == "sim":
        if not config['SIM_BROKER_DB'] or not Path(config['SIM_BROKER_DB']).is_file():
            raise RuntimeError("SIM_BROKER_DB must point to an initialized v2 simulation database")
        uri = Path(config['SIM_BROKER_DB']).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            rows = conn.execute("SELECT rowid,name FROM sim_account ORDER BY rowid").fetchall()
        if any(name != name.lower() for _, name in rows):
            raise RuntimeError("simulated account names must be lowercase for legacy webhook routing")
        config['ACCOUNTS'] = {name: 900000 + rowid for rowid, name in rows}
    else:
        config['ACCOUNTS'] = {
            k[len("ACCOUNT_"):].lower(): int(v)
            for k, v in os.environ.items() if k.startswith("ACCOUNT_")
        }
    config['INVERTED_SIGNAL_ROUTING'] = {
        k[len("INVERT_TO_"):].lower(): v.lower()
        for k, v in os.environ.items() if k.startswith("INVERT_TO_") and v
    }
    if not config['ACCOUNTS']:
        raise RuntimeError("No accounts loaded from .env. Add ACCOUNT_<NAME>=<ID>.")
    config['DEFAULT_ACCOUNT'] = next(iter(config['ACCOUNTS']))
    config['OVERRIDE_CONTRACT_ID'] = (
        "CON.F.US.MES.SIM" if broker_mode == "sim"
        else os.getenv("OVERRIDE_CONTRACT_ID", "CON.F.US.MES.H26")
    )
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
