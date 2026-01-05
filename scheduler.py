#scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
import requests
from config import load_config
from api import flatten_contract, search_pos

config = load_config()
WEBHOOK_SECRET = config['WEBHOOK_SECRET']
LOCAL_TZ = config['MT']
TV_PORT = config['TV_PORT']
ACCOUNTS = config['ACCOUNTS']
OVERRIDE_CONTRACT_ID = config['OVERRIDE_CONTRACT_ID']
N8N_CHART_ENDPOINTS = {
    "5m": config.get('N8N_5MCHART_FETCH_URL') or config.get('N8N_CHART_FETCH_URL'),
    "15m": config.get('N8N_15MCHART_FETCH_URL'),
    "30m": config.get('N8N_30MCHART_FETCH_URL'),
}
N8N_SCHEDULED_FLOW_ENDPOINTS = {
    "delta": config.get("N8N_OVERSEER_URL_TEST4") or config.get("N8N_AI_URL"),
    "epsilon": config.get("N8N_OVERSEER_URL_TEST5") or config.get("N8N_AI_URL"),
}

def start_scheduler(app):
    scheduler = BackgroundScheduler()

    def flatten_all_open_positions():
        for acct_name, acct_id in ACCOUNTS.items():
            positions = search_pos(acct_id)
            if not positions:
                logging.info("[APScheduler] %s: no open positions to flatten", acct_name)
                continue

            contract_ids = {
                pos.get("contractId") or pos.get("contractSymbol")
                for pos in positions
                if pos.get("contractId") or pos.get("contractSymbol")
            }

            if not contract_ids:
                logging.info("[APScheduler] %s: could not determine contract ids from positions", acct_name)
                continue

            for cid in contract_ids:
                logging.info("[APScheduler] Flattening %s for account %s", cid, acct_name)
                flatten_contract(acct_id, cid, timeout=10)

    def chart_prefetch_job(timeframes):
        configured = {
            tf: url
            for tf, url in N8N_CHART_ENDPOINTS.items()
            if url and tf in timeframes
        }
        if not configured:
            logging.warning(
                "[APScheduler] N8N chart fetch URLs not configured for %s; skipping chart prefetch",
                ",".join(sorted(timeframes)),
            )
            return

        for timeframe, url in configured.items():
            payload = {
                "symbol": "MES",
                "timeframe": timeframe,
                "source": "scheduler",
                "secret": WEBHOOK_SECRET,
            }
            try:
                resp = requests.post(url, json=payload, timeout=30)
                snippet = resp.text[:120]
                logging.info(
                    "[APScheduler] Chart prefetch timeframe=%s status=%s body=%s",
                    timeframe,
                    resp.status_code,
                    snippet,
                )
            except Exception as exc:
                logging.error("[APScheduler] Chart prefetch failed for %s: %s", timeframe, exc)

    def overseer_job():
        symbol = OVERRIDE_CONTRACT_ID or "CON.F.US.MES.H26"
        target_accounts = [acct for acct in ("alpha", "beta", "gamma") if acct in ACCOUNTS]

        if not target_accounts:
            logging.warning("[APScheduler] No target accounts configured for overseer job")
            return

        for acct in target_accounts:
            data = {
                "secret": WEBHOOK_SECRET,
                "strategy": "",
                "account": acct,
                "signal": "",
                "symbol": symbol,
                "size": 3,
                "alert": "APScheduler 5m overseer",
            }
            try:
                response = requests.post(
                    f'http://localhost:{TV_PORT}/webhook',
                    json=data,
                    timeout=10,
                )
                snippet = response.text[:120]
                logging.info(
                    "[APScheduler] Overseer call account=%s status=%s body=%s",
                    acct,
                    response.status_code,
                    snippet,
                )
            except Exception as exc:
                logging.error("[APScheduler] Overseer call failed for %s: %s", acct, exc)

    def run_n8n_flow(account: str, timeframe: str):
        url = N8N_SCHEDULED_FLOW_ENDPOINTS.get(account)
        if not url:
            logging.warning(
                "[APScheduler] n8n flow for %s (%s) not configured; skipping",
                account,
                timeframe,
            )
            return

        payload = {
            "symbol": "MES",
            "timeframe": timeframe,
            "source": f"scheduler-{account}",
            "secret": WEBHOOK_SECRET,
            "account": account,
        }

        try:
            resp = requests.post(url, json=payload, timeout=30)
            snippet = resp.text[:120]
            logging.info(
                "[APScheduler] n8n flow account=%s timeframe=%s status=%s body=%s",
                account,
                timeframe,
                resp.status_code,
                snippet,
            )
        except Exception as exc:
            logging.error(
                "[APScheduler] n8n flow failed for %s (%s): %s", account, timeframe, exc
            )

    scheduler.add_job(
        chart_prefetch_job,
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=0, timezone=LOCAL_TZ),
        id='chart_prefetch_job_5m',
        args=[{"5m"}],
        replace_existing=True
    )

    scheduler.add_job(
        chart_prefetch_job,
        CronTrigger(minute='0,15,30,45', second=0, timezone=LOCAL_TZ),
        id='chart_prefetch_job_15m',
        args=[{"15m"}],
        replace_existing=True
    )

    scheduler.add_job(
        chart_prefetch_job,
        CronTrigger(minute='0,30', second=0, timezone=LOCAL_TZ),
        id='chart_prefetch_job_30m',
        args=[{"30m"}],
        replace_existing=True
    )

    scheduler.add_job(
        run_n8n_flow,
        CronTrigger(minute='0,15,30,45', second=15, timezone=LOCAL_TZ),
        id='n8n_delta_flow_15m',
        args=["delta", "15m"],
        replace_existing=True,
    )

    scheduler.add_job(
        run_n8n_flow,
        CronTrigger(minute='0,30', second=15, timezone=LOCAL_TZ),
        id='n8n_epsilon_flow_30m',
        args=["epsilon", "30m"],
        replace_existing=True,
    )

    scheduler.add_job(
        overseer_job,
        CronTrigger(minute='0,5,10,15,20,25,30,35,40,45,50,55', second=15, timezone=LOCAL_TZ),
        id='overseer_job',
        replace_existing=True
    )

    scheduler.add_job(
        flatten_all_open_positions,
        CronTrigger(day_of_week='mon-fri', hour=14, minute=5, timezone=LOCAL_TZ),
        id='force_flat_job',
        replace_existing=True,
    )
    scheduler.start()
    logging.info("[APScheduler] Scheduler started with chart prefetch and overseer jobs.")
    return scheduler
