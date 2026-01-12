# scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
from datetime import datetime
import requests
import subprocess
import sys
from pathlib import Path

from config import load_config
from api import flatten_contract, search_pos
from auth import in_get_flat


config = load_config()
WEBHOOK_SECRET = config["WEBHOOK_SECRET"]
LOCAL_TZ = config["MT"]
TV_PORT = config["TV_PORT"]
ACCOUNTS = config["ACCOUNTS"]
OVERRIDE_CONTRACT_ID = config["OVERRIDE_CONTRACT_ID"]

N8N_CHART_ENDPOINTS = {
    "5m": config.get("N8N_5MCHART_FETCH_URL"),
    "15m": config.get("N8N_15MCHART_FETCH_URL"),
    "30m": config.get("N8N_30MCHART_FETCH_URL"),
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
        now = datetime.now(LOCAL_TZ)
        if in_get_flat(now):
            logging.info("[APScheduler] In get-flat window; skipping chart prefetch")
            return

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
                snippet = (resp.text or "")[:120]
                logging.info(
                    "[APScheduler] Chart prefetch timeframe=%s status=%s body=%s",
                    timeframe,
                    resp.status_code,
                    snippet,
                )
            except Exception as exc:
                logging.error("[APScheduler] Chart prefetch failed for %s: %s", timeframe, exc)

    def trigger_overseer(account: str, timeframe_label: str):
        now = datetime.now(LOCAL_TZ)
        if in_get_flat(now):
            logging.info(
                "[APScheduler] Skipping overseer for %s (%s) during get-flat window",
                account,
                timeframe_label,
            )
            return

        if account not in ACCOUNTS:
            logging.warning("[APScheduler] Unknown account %s for overseer trigger", account)
            return

        symbol = OVERRIDE_CONTRACT_ID or "CON.F.US.MES.H26"
        data = {
            "secret": WEBHOOK_SECRET,
            "strategy": "",
            "account": account,
            "signal": "",
            "symbol": symbol,
            "size": 3,
            "alert": f"APScheduler {timeframe_label} overseer",
        }

        try:
            response = requests.post(
                f"http://localhost:{TV_PORT}/webhook",
                json=data,
                timeout=10,
            )
            snippet = (response.text or "")[:120]
            logging.info(
                "[APScheduler] Overseer call account=%s timeframe=%s status=%s body=%s",
                account,
                timeframe_label,
                response.status_code,
                snippet,
            )
        except Exception as exc:
            logging.error(
                "[APScheduler] Overseer call failed for %s (%s): %s",
                account,
                timeframe_label,
                exc,
            )

    def run_daily_artifacts():
        """
        Calls:
          reports/run_daily_reports.py
          reports/upload_daily_report.py
        """
        try:
            base = Path(__file__).resolve().parent
            reports_dir = base / "reports"
            run_script = reports_dir / "run_daily_reports.py"
            upload_script = reports_dir / "upload_daily_report.py"

            if not run_script.exists() or not upload_script.exists():
                logging.warning(
                    "[APScheduler] Daily report scripts missing. run=%s exists=%s upload=%s exists=%s",
                    run_script,
                    run_script.exists(),
                    upload_script,
                    upload_script.exists(),
                )
                return

            subprocess.run([sys.executable, str(run_script)], check=True)
            subprocess.run([sys.executable, str(upload_script)], check=True)
            logging.info("[APScheduler] Daily report generated + uploaded.")
        except Exception as exc:
            logging.exception("[APScheduler] Daily report pipeline failed: %s", exc)

    # --- Jobs (now safe because functions are defined above) ---

    scheduler.add_job(
        chart_prefetch_job,
        CronTrigger(minute="0,5,10,15,20,25,30,35,40,45,50,55", second=0, timezone=LOCAL_TZ),
        id="chart_prefetch_job_5m",
        args=[{"5m"}],
        replace_existing=True,
    )

    scheduler.add_job(
        chart_prefetch_job,
        CronTrigger(minute="0,15,30,45", second=0, timezone=LOCAL_TZ),
        id="chart_prefetch_job_15m",
        args=[{"15m"}],
        replace_existing=True,
    )

    scheduler.add_job(
        chart_prefetch_job,
        CronTrigger(minute="0,30", second=0, timezone=LOCAL_TZ),
        id="chart_prefetch_job_30m",
        args=[{"30m"}],
        replace_existing=True,
    )   

    '''
    if "alpha" in ACCOUNTS:
        scheduler.add_job(
            trigger_overseer,
            CronTrigger(minute="0,5,10,15,20,25,30,35,40,45,50,55", second=15, timezone=LOCAL_TZ),
            id="overseer_job_5m_alpha",
            args=["alpha", "5m"],
            replace_existing=True,
        )
        
    if "beta" in ACCOUNTS:
        scheduler.add_job(
            trigger_overseer,
            CronTrigger(minute="0,5,10,15,20,25,30,35,40,45,50,55", second=15, timezone=LOCAL_TZ),
            id="overseer_job_5m_beta",
            args=["beta", "5m"],
            replace_existing=True,
        )
        
    if "gamma" in ACCOUNTS:
        scheduler.add_job(
            trigger_overseer,
            CronTrigger(minute="0,5,10,15,20,25,30,35,40,45,50,55", second=15, timezone=LOCAL_TZ),
            id="overseer_job_5m_gamma",
            args=["gamma", "5m"],
            replace_existing=True,
        )
    '''
    if "practice" in ACCOUNTS:
        scheduler.add_job(
            trigger_overseer,
            CronTrigger(minute="0,5,10,15,20,25,30,35,40,45,50,55", second=15, timezone=LOCAL_TZ),
            id="overseer_job_5m_practice",
            args=["practice", "5m"],
            replace_existing=True,
        )
    if "sim001" in ACCOUNTS:
        scheduler.add_job(
            trigger_overseer,
            CronTrigger(minute="0,5,10,15,20,25,30,35,40,45,50,55", second=15, timezone=LOCAL_TZ),
            id="overseer_job_5m_sim001",
            args=["sim001", "5m"],
            replace_existing=True,
        )
    '''
    if "delta" in ACCOUNTS:
        scheduler.add_job(
            trigger_overseer,
            CronTrigger(minute="0,15,30,45", second=15, timezone=LOCAL_TZ),
            id="overseer_job_15m_delta",
            args=["delta", "15m"],
            replace_existing=True,
        )
    '''
    if "epsilon" in ACCOUNTS:
        scheduler.add_job(
            trigger_overseer,
            CronTrigger(minute="0,30", second=15, timezone=LOCAL_TZ),
            id="overseer_job_30m_epsilon",
            args=["epsilon", "30m"],
            replace_existing=True,
        )

    scheduler.add_job(
        flatten_all_open_positions,
        CronTrigger(day_of_week="mon-fri", hour=14, minute=5, timezone=LOCAL_TZ),
        id="force_flat_job",
        replace_existing=True,
    )

    scheduler.add_job(
        run_daily_artifacts,
        CronTrigger(day_of_week="mon-fri", hour=14, minute=10, timezone=LOCAL_TZ),
        id="daily_report_job",
        replace_existing=True,
    )

    scheduler.start()
    logging.info("[APScheduler] Scheduler started with chart prefetch and overseer jobs.")
    return scheduler
