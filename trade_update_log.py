import os
import requests
import datetime
import pytz
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
PX_BASE         = os.getenv("PROJECTX_BASE_URL")
USER_NAME       = os.getenv("PROJECTX_USERNAME")
API_KEY         = os.getenv("PROJECTX_API_KEY")
SUPABASE_URL    = os.getenv("SUPABASE_URL")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

ACCOUNTS = {k[len("ACCOUNT_"):].lower(): int(v)
    for k, v in os.environ.items() if k.startswith("ACCOUNT_")}

def get_token():
    r = requests.post(
        f"{PX_BASE}/api/Auth/loginKey",
        json={"userName": USER_NAME, "apiKey": API_KEY},
        headers={"Content-Type": "application/json"},
        timeout=(3.05, 10)
    )
    r.raise_for_status()
    return r.json()["token"]

token = get_token()
session = requests.Session()
session.headers.update({
    "Content-Type": "application/json",
    "Authorization": f"Bearer {token}",
})

def get_logged_trade_ids(days=2):
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat()
    rows = supabase.table('trade_results').select("order_id,entry_time").gte('entry_time', cutoff).execute()
    seen = set()
    for r in rows.data:
        if r.get('order_id'):
            seen.add(str(r['order_id']))
    return seen

def get_recent_trades(acct_id, days=2):
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).replace(tzinfo=pytz.UTC)
    resp = session.post(
        f"{PX_BASE}/api/Trade/search",
        json={"accountId": acct_id, "startTimestamp": since.isoformat()},
        timeout=(3.05, 10)
    )
    resp.raise_for_status()
    return resp.json().get('trades', [])

def get_ai_trading_log(days=2):
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat()
    rows = supabase.table('ai_trading_log').select("*").gte('timestamp', cutoff).execute()
    return rows.data

def find_best_ai_log_match(trade, ai_logs):
    # Match by order_id first if present
    tid = str(trade.get('orderId') or "")
    for log in ai_logs:
        if str(log.get('order_id', "")) == tid:
            return log
    # Otherwise, match by entry_time and symbol (fuzzy, within 2 minutes)
    entry_time = trade.get('entryTimestamp')
    symbol = trade.get('contractId')
    trade_time = None
    try:
        trade_time = datetime.datetime.fromisoformat(entry_time.replace('Z', '+00:00'))
    except Exception:
        return None
    for log in ai_logs:
        log_time = None
        try:
            log_time = datetime.datetime.fromisoformat(log.get('timestamp').replace('Z', '+00:00'))
        except Exception:
            continue
        if log.get('symbol') == symbol and abs((trade_time - log_time).total_seconds()) < 120:
            return log
    return None

def main():
    days = 2
    logged = get_logged_trade_ids(days=days)
    ai_logs = get_ai_trading_log(days=days)
    print(f"Loaded {len(logged)} logged trades from Supabase.")
    print(f"Loaded {len(ai_logs)} ai_trading_log records.")

    for acct_name, acct_id in ACCOUNTS.items():
        print(f"\nChecking account: {acct_name} ({acct_id})")
        trades = get_recent_trades(acct_id, days=days)
        for trade in trades:
            order_id = str(trade.get('orderId') or "")
            contract_id = trade.get('contractId')
            if contract_id != "CON.F.US.MES.M25":
                continue
            if order_id in logged:
                continue
            if trade.get('voided', False):
                continue

            ai_log = find_best_ai_log_match(trade, ai_logs)
            # Merge fields
            payload = {
                "ai_decision_id": ai_log.get("id") if ai_log else None,
                "order_id": order_id,
                "symbol": contract_id,
                "account": acct_id,
                "strategy": ai_log.get("strategy") if ai_log else None,
                "signal": ai_log.get("signal") if ai_log else None,
                "entry_time": trade.get('entryTimestamp'),
                "exit_time": trade.get('exitTimestamp'),
                "duration_sec": trade.get('durationSec'),
                "size": trade.get('size'),
                "total_pnl": trade.get('profitAndLoss'),
                "alert": ai_log.get("alert") if ai_log else None,
                "raw_trades": [trade],
                "comment": f"Catchup script: backfilled from TopstepX; AI log merged: {bool(ai_log)}",
            }
            print(f"Uploading missing trade: {order_id}, pnl={trade.get('profitAndLoss')}, entry={trade.get('entryTimestamp')}, ai_log={'YES' if ai_log else 'NO'}")
            try:
                supabase.table('trade_results').insert(payload).execute()
                print("...Uploaded.")
            except Exception as e:
                print(f"...Upload failed: {e}")

if __name__ == "__main__":
    main()
