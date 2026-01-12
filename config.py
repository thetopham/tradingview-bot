# config.py
import os
import json
from dotenv import load_dotenv
from datetime import time as dtime
import pytz

def load_config():
    load_dotenv()
    sim_default_balance = float(os.getenv("SIM_DEFAULT_BALANCE", 50000))
    sim_default_sl = float(os.getenv("SIM_DEFAULT_SL_USD", 30))
    sim_default_tp = float(os.getenv("SIM_DEFAULT_TP_USD", 60))
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
        'N8N_OVERSEER_URL_TEST7': os.getenv("N8N_OVERSEER_URL_TEST7"),
        'SUPABASE_URL': os.getenv("SUPABASE_URL"),
        'SUPABASE_KEY': os.getenv("SUPABASE_KEY"),
        'WEBHOOK': os.getenv("WEBHOOK"),
        'BROKER_MODE': os.getenv("BROKER_MODE", "live"),
        'SIM_STATE_PATH': os.getenv("SIM_STATE_PATH", "./simbroker_state.json"),
        'SIM_DEFAULT_BALANCE': sim_default_balance,
        'SIM_DEFAULT_SL_USD': sim_default_sl,
        'SIM_DEFAULT_TP_USD': sim_default_tp,
        'SIM_RISK_BASIS': os.getenv("SIM_RISK_BASIS", "per_position"),
        'SIM_FILL_POLICY': os.getenv("SIM_FILL_POLICY", "worst"),
        'SIM_STARTING_BALANCE': float(os.getenv("SIM_STARTING_BALANCE", sim_default_balance)),
        'SIM_BRACKET_SL_USD': float(os.getenv("SIM_BRACKET_SL_USD", sim_default_sl)),
        'SIM_BRACKET_TP_USD': float(os.getenv("SIM_BRACKET_TP_USD", sim_default_tp)),
        'SIM_PRICE_TIMEFRAME': os.getenv("SIM_PRICE_TIMEFRAME", "1m"),
        'SIM_DEFAULT_TICK_SIZE': float(os.getenv("SIM_DEFAULT_TICK_SIZE", 0.25)),
        'SIM_DEFAULT_TICK_VALUE': float(os.getenv("SIM_DEFAULT_TICK_VALUE", 1.25)),
        # Risk params
        'DAILY_PROFIT_TARGET': float(os.getenv("DAILY_PROFIT_TARGET", 99999.0)),
        'MAX_DAILY_LOSS': float(os.getenv("MAX_DAILY_LOSS", -250.0)),
        'MAX_CONSECUTIVE_LOSSES': int(os.getenv("MAX_CONSECUTIVE_LOSSES", 99999)),
    }
    config['SIM_ACCOUNTS_JSON'] = os.getenv("SIM_ACCOUNTS_JSON")
    config['SIM_ACCOUNTS_PATH'] = os.getenv("SIM_ACCOUNTS_PATH")

    # Build account map
    config['ACCOUNTS'] = {
        k[len("ACCOUNT_"):].lower(): int(v)
        for k, v in os.environ.items() if k.startswith("ACCOUNT_")
    }
    sim_account_settings_by_id = {}
    sim_accounts = {}
    sim_accounts_json = (config.get("SIM_ACCOUNTS_JSON") or "").strip()
    sim_accounts_path = (config.get("SIM_ACCOUNTS_PATH") or "").strip()
    if config['BROKER_MODE'] == "sim" or sim_accounts_json or sim_accounts_path:
        if sim_accounts_json:
            try:
                sim_accounts = json.loads(sim_accounts_json)
            except json.JSONDecodeError as exc:
                raise RuntimeError("SIM_ACCOUNTS_JSON must be valid JSON.") from exc
        elif sim_accounts_path:
            try:
                with open(sim_accounts_path, "r", encoding="utf-8") as handle:
                    sim_accounts = json.load(handle)
            except FileNotFoundError as exc:
                raise RuntimeError("SIM_ACCOUNTS_PATH not found.") from exc
            except json.JSONDecodeError as exc:
                raise RuntimeError("SIM_ACCOUNTS_PATH must contain valid JSON.") from exc
        if sim_accounts and not isinstance(sim_accounts, dict):
            raise RuntimeError("SIM_ACCOUNTS_JSON or SIM_ACCOUNTS_PATH must be a JSON object.")

        for account_name, account_config in sim_accounts.items():
            if not isinstance(account_config, dict):
                raise RuntimeError(f"Sim account config for {account_name} must be an object.")
            account_id = account_config.get("id")
            if account_id is None:
                raise RuntimeError(f"Sim account {account_name} is missing id.")
            account_id_int = int(account_id)
            config['ACCOUNTS'][str(account_name).lower()] = account_id_int
            sim_account_settings_by_id[account_id_int] = {
                "name": str(account_name),
                "balance": float(account_config.get("balance", config['SIM_DEFAULT_BALANCE'])),
                "sl_usd": float(account_config.get("sl_usd", config['SIM_DEFAULT_SL_USD'])),
                "tp_usd": float(account_config.get("tp_usd", config['SIM_DEFAULT_TP_USD'])),
                "risk_basis": config['SIM_RISK_BASIS'],
                "fill_policy": config['SIM_FILL_POLICY'],
            }

    if not config['ACCOUNTS']:
        raise RuntimeError("No accounts loaded from .env. Add ACCOUNT_<NAME>=<ID>.")
    default_account_override = os.getenv("DEFAULT_ACCOUNT")
    if default_account_override and default_account_override.lower() in config['ACCOUNTS']:
        config['DEFAULT_ACCOUNT'] = default_account_override.lower()
    else:
        config['DEFAULT_ACCOUNT'] = next(iter(config['ACCOUNTS']))
    default_sl_usd = config['SIM_BRACKET_SL_USD']
    default_tp_usd = config['SIM_BRACKET_TP_USD']
    sim_account_brackets = {}
    for account_name in config['ACCOUNTS'].keys():
        env_prefix = f"SIM_ACCOUNT_{str(account_name).upper()}"
        sl_env = os.getenv(f"{env_prefix}_SL_USD")
        tp_env = os.getenv(f"{env_prefix}_TP_USD")
        try:
            sl_usd = float(sl_env) if sl_env not in (None, "") else default_sl_usd
        except ValueError:
            sl_usd = default_sl_usd
        try:
            tp_usd = float(tp_env) if tp_env not in (None, "") else default_tp_usd
        except ValueError:
            tp_usd = default_tp_usd
        sim_account_brackets[str(account_name).lower()] = {
            "sl_usd": sl_usd,
            "tp_usd": tp_usd,
        }
    config['SIM_ACCOUNT_BRACKETS'] = sim_account_brackets
    config['SIM_ACCOUNT_SETTINGS_BY_ID'] = sim_account_settings_by_id
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
