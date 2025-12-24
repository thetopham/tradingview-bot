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

        # legacy single/dual endpoints (kept for backward-compat)
        'N8N_AI_URL': os.getenv("N8N_AI_URL"),
        'N8N_AI_URL2': os.getenv("N8N_AI_URL2"),

        'SUPABASE_URL': os.getenv("SUPABASE_URL"),
        'SUPABASE_KEY': os.getenv("SUPABASE_KEY"),
        'WEBHOOK': os.getenv("WEBHOOK"),

        # Risk params
        'DAILY_PROFIT_TARGET': float(os.getenv("DAILY_PROFIT_TARGET", 500.0)),
        'MAX_DAILY_LOSS': float(os.getenv("MAX_DAILY_LOSS", -500.0)),
        'MAX_CONSECUTIVE_LOSSES': int(os.getenv("MAX_CONSECUTIVE_LOSSES", 3)),
        'STOP_LOSS_POINTS': float(os.getenv("STOP_LOSS_POINTS", 10.0)),
        'TP_POINTS': [float(x) for x in os.getenv("TP_POINTS", "2.5,5.0,10.0").split(",")],
    }

    # Mode/symbol
    config['USE_DYNAMIC_CONTRACTS'] = os.getenv("USE_DYNAMIC_CONTRACTS", "true").lower() == "true"
    config['LIVE_MODE'] = os.getenv("LIVE_MODE", "false").lower() == "true"
    config['DEFAULT_SYMBOL'] = os.getenv("DEFAULT_SYMBOL", "MES")

    # Bracket templates (server-side)
    config['BRACKET_TEMPLATE_DEFAULT'] = os.getenv("BRACKET_TEMPLATE_DEFAULT", "")
    config['BRACKET_TEMPLATES'] = {
        k[len("BRACKET_TEMPLATE_"):].lower(): v
        for k, v in os.environ.items()
        if k.startswith("BRACKET_TEMPLATE_")
    }

    # Accounts (ACCOUNT_ALPHA=12345, etc.)
    config['ACCOUNTS'] = {
        k[len("ACCOUNT_"):].lower(): int(v)
        for k, v in os.environ.items()
        if k.startswith("ACCOUNT_")
    }
    if not config['ACCOUNTS']:
        raise RuntimeError("No accounts loaded from .env. Add ACCOUNT_<NAME>=<ID>.")
    config['DEFAULT_ACCOUNT'] = next(iter(config['ACCOUNTS']))

    # NEW: per-account AI endpoints (N8N_AI_URL_ALPHA=..., etc.)
    ai_eps = {
        'alpha':   os.getenv('N8N_AI_URL_ALPHA'),
        'beta':    os.getenv('N8N_AI_URL_BETA'),
        'gamma':   os.getenv('N8N_AI_URL_GAMMA'),
        'delta':   os.getenv('N8N_AI_URL_DELTA'),
        'epsilon': os.getenv('N8N_AI_URL_EPSILON'),
    }
    # prune Nones so you can mix legacy + new during transition
    config['AI_ENDPOINTS'] = {k: v for k, v in ai_eps.items() if v}

    # Fallback: if no per-account map provided, fall back to legacy var(s)
    if not config['AI_ENDPOINTS']:
        # Map first two accounts to legacy URLs if present
        acct_names = list(config['ACCOUNTS'].keys())
        legacy_map = {}
        if config['N8N_AI_URL'] and acct_names:
            legacy_map[acct_names[0]] = config['N8N_AI_URL']
        if config['N8N_AI_URL2'] and len(acct_names) > 1:
            legacy_map[acct_names[1]] = config['N8N_AI_URL2']
        if legacy_map:
            config['AI_ENDPOINTS'] = legacy_map
        else:
            raise RuntimeError("No AI endpoints configured. Set N8N_AI_URL_<ACCOUNT> or N8N_AI_URL.")

    # Trading day windows / tz
    config['OVERRIDE_CONTRACT_ID'] = os.getenv("OVERRIDE_CONTRACT_ID", None)
    config['GET_FLAT_START'] = dtime(15, 7)
    config['GET_FLAT_END'] = dtime(17, 0)
    config['CT'] = pytz.timezone("America/Chicago")
    return config
