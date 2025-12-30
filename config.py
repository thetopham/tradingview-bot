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

        # Overseer / automation
        'N8N_OVERSEER_URL': os.getenv("N8N_OVERSEER_URL", ""),
        'AUTOTRADE_ENABLED': os.getenv("AUTOTRADE_ENABLED", "false").lower() == "true",
        'AUTOTRADE_SIZE': int(os.getenv("AUTOTRADE_SIZE", 1)),
        'AUTOTRADE_REQUIRE_CONFLUENCE': os.getenv("AUTOTRADE_REQUIRE_CONFLUENCE", "true").lower() == "true",
        'AUTOTRADE_MIN_SCORE': float(os.getenv("AUTOTRADE_MIN_SCORE", 1.0)),
        'AUTOTRADE_MIN_SCORE_SCALP': float(os.getenv("AUTOTRADE_MIN_SCORE_SCALP", 0.6)),

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
        'DASHBOARD_DIAGNOSTICS_PUBLIC': os.getenv("DASHBOARD_DIAGNOSTICS_PUBLIC", "false").lower() == "true",
    }

    # Mode/symbol
    config['USE_DYNAMIC_CONTRACTS'] = os.getenv("USE_DYNAMIC_CONTRACTS", "true").lower() == "true"
    config['LIVE_MODE'] = os.getenv("LIVE_MODE", "false").lower() == "true"
    config['DEFAULT_SYMBOL'] = os.getenv("DEFAULT_SYMBOL", "MES")

    # Accounts (ACCOUNT_ALPHA=12345, etc.)
    config['ACCOUNTS'] = {
        k[len("ACCOUNT_"):].lower(): int(v)
        for k, v in os.environ.items()
        if k.startswith("ACCOUNT_")
    }
    if not config['ACCOUNTS']:
        raise RuntimeError("No accounts loaded from .env. Add ACCOUNT_<NAME>=<ID>.")

    # Optional allowlist to keep the bot focused on a single account (e.g., beta)
    allowlist_raw = os.getenv("ACTIVE_ACCOUNTS")
    if allowlist_raw:
        allowed = {
            name.strip().lower()
            for name in allowlist_raw.split(",")
            if name.strip()
        }
        config['ACCOUNTS'] = {
            name: acct_id
            for name, acct_id in config['ACCOUNTS'].items()
            if name in allowed
        }
        if not config['ACCOUNTS']:
            raise RuntimeError(
                "ACTIVE_ACCOUNTS filtered out all accounts. Check your account names."
            )

    config['DEFAULT_ACCOUNT'] = next(iter(config['ACCOUNTS']))

    # Autotrade allowlist (defaults to all known accounts)
    autotrade_accounts_raw = os.getenv("AUTOTRADE_ACCOUNTS")
    if autotrade_accounts_raw:
        autotrade_accounts = [
            name.strip().lower()
            for name in autotrade_accounts_raw.split(",")
            if name.strip()
        ]
        config['AUTOTRADE_ACCOUNTS'] = [
            name for name in autotrade_accounts if name in config['ACCOUNTS']
        ]
    else:
        config['AUTOTRADE_ACCOUNTS'] = list(config['ACCOUNTS'].keys())

    # NEW: per-account AI endpoints (N8N_AI_URL_ALPHA=..., etc.)
    ai_eps = {
        'alpha':   os.getenv('N8N_AI_URL_ALPHA'),
        'beta':    os.getenv('N8N_AI_URL_BETA'),
        'gamma':   os.getenv('N8N_AI_URL_GAMMA'),
        'delta':   os.getenv('N8N_AI_URL_DELTA'),
        'epsilon': os.getenv('N8N_AI_URL_EPSILON'),
    }
    # prune Nones so you can mix legacy + new during transition
    config['AI_ENDPOINTS'] = {
        k: v
        for k, v in ai_eps.items()
        if v and k in config['ACCOUNTS']
    }

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
            config['AI_ENDPOINTS'] = {}

    # Final guard: keep AI endpoints aligned with the active account list
    config['AI_ENDPOINTS'] = {
        name: url for name, url in config['AI_ENDPOINTS'].items()
        if name in config['ACCOUNTS']
    }

    # Trading day windows / tz
    config['OVERRIDE_CONTRACT_ID'] = os.getenv("OVERRIDE_CONTRACT_ID", None)
    config['GET_FLAT_START'] = dtime(15, 7)
    config['GET_FLAT_END'] = dtime(17, 0)
    config['CT'] = pytz.timezone("America/Chicago")
    return config
