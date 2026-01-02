# config.py
import os
from dotenv import load_dotenv
from datetime import time as dtime
import pytz

def load_config():
    load_dotenv()
    config = {
        'TV_PORT': int(os.getenv("TV_PORT", 5000)),
        'PX_BASE': os.getenv("PROJECTX_BASE_URL"),
        'USER_NAME': os.getenv("PROJECTX_USERNAME"),
        'API_KEY': os.getenv("PROJECTX_API_KEY"),
        'WEBHOOK_SECRET': os.getenv("WEBHOOK_SECRET"),
        'N8N_AI_URL': os.getenv("N8N_AI_URL"),
        'N8N_AI_URL2': os.getenv("N8N_AI_URL2"),
        'SUPABASE_URL': os.getenv("SUPABASE_URL"),
        'SUPABASE_KEY': os.getenv("SUPABASE_KEY"),
        'WEBHOOK': os.getenv("WEBHOOK"),
        # Risk params
        'DAILY_PROFIT_TARGET': float(os.getenv("DAILY_PROFIT_TARGET", 99999.0)),
        'MAX_DAILY_LOSS': float(os.getenv("MAX_DAILY_LOSS", -250.0)),
        'MAX_CONSECUTIVE_LOSSES': int(os.getenv("MAX_CONSECUTIVE_LOSSES", 99999)),
    }
    # Build account map
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

    # Trading hours are defined in Mountain Time (America/Denver)
    config['GET_FLAT_START'] = dtime(14, 5)  # 2:05pm MT
    config['GET_FLAT_END'] = dtime(16, 0)    # 4:00pm MT
    # Markets stay flat on Saturday and reopen Sunday at 3:00pm MT
    config['WEEKEND_MARKET_OPEN'] = dtime(15, 0)
    config['MT'] = mountain
    return config

