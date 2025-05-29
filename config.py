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
        
        # ADD THESE LINES - Load risk management parameters
        'DAILY_PROFIT_TARGET': float(os.getenv("DAILY_PROFIT_TARGET", 500.0)),
        'MAX_DAILY_LOSS': float(os.getenv("MAX_DAILY_LOSS", -500.0)),
        'MAX_CONSECUTIVE_LOSSES': int(os.getenv("MAX_CONSECUTIVE_LOSSES", 3)),
        
        # Add stop loss and take profit parameters too
        'STOP_LOSS_POINTS': float(os.getenv("STOP_LOSS_POINTS", 10.0)),
        'TP_POINTS': [float(x) for x in os.getenv("TP_POINTS", "2.5,5.0,10.0").split(",")],
    }

    # Contract configuration
    config['USE_DYNAMIC_CONTRACTS'] = os.getenv("USE_DYNAMIC_CONTRACTS", "true").lower() == "true"
    config['LIVE_MODE'] = os.getenv("LIVE_MODE", "false").lower() == "true"
    config['DEFAULT_SYMBOL'] = os.getenv("DEFAULT_SYMBOL", "MES")

    # Build account map
    config['ACCOUNTS'] = {
        k[len("ACCOUNT_"):].lower(): int(v)
        for k, v in os.environ.items() if k.startswith("ACCOUNT_")
    }
    if not config['ACCOUNTS']:
        raise RuntimeError("No accounts loaded from .env. Add ACCOUNT_<NAME>=<ID>.")
    config['DEFAULT_ACCOUNT'] = next(iter(config['ACCOUNTS']))
    config['OVERRIDE_CONTRACT_ID'] = os.getenv("OVERRIDE_CONTRACT_ID", None)
    config['GET_FLAT_START'] = dtime(15, 7)
    config['GET_FLAT_END'] = dtime(17, 0)
    config['CT'] = pytz.timezone("America/Chicago")
    return config
