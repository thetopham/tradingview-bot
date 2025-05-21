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
    # Fuzzy match by symbol and closest timestamp (within 2 min) to creationTimestamp
    symbol = trade.get('contractId')
    trade_time_str = trade.get('creationTimestamp')
    trade_time = None
    try:
        trade_time = datetime.datetime.fromisoformat(trade_time_str.replace('Z', '+00:00'))
    except Exception:
        return None
    closest_log = None
    min_diff = 120  # seconds
    for log in ai_logs:
        if log.get('symbol') != symbol:
            continue
        log_time = None
        try:
            log_time = datetime.datetime.fromisoformat(log.get('timestamp').replace('Z', '+00:00'))
        except Exception:
            continue
        diff = abs((trade_time - log_time).total_seconds())
        if diff < min_diff:
            min_diff = diff
            closest_log = log
    return closest_log

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
            # Merge fields, set times and duration as per available data
            payload = {
                "ai_decision_id": ai_log.get("ai_decision_id") if ai_log else None,
                "order_id": order_id,
                "symbol": contract_id,
                "account": acct_id,
                "strategy": ai_log.get("strategy") if ai_log else None,
                "signal": ai_log.get("signal") if ai_log else None,
                "entry_time": trade.get('creationTimestamp'),
                "exit_time": trade.get('creationTimestamp'),
                "duration_sec": 0,
                "size": trade.get('size'),
                "total_pnl": trade.get('profitAndLoss'),
                "alert": ai_log.get("alert") if ai_log else None,
                "raw_trades": [trade],
                "comment": f"Catchup script: TopstepX; Only creationTimestamp present. AI log merged: {bool(ai_log)}",
            }
            print(f"Uploading missing trade: {order_id}, pnl={trade.get('profitAndLoss')}, entry={trade.get('creationTimestamp')}, ai_log={'YES' if ai_log else 'NO'}")
            try:
                supabase.table('trade_results').insert(payload).execute()
                print("...Uploaded.")
            except Exception as e:
                print(f"...Upload failed: {e}")

if __name__ == "__main__":
    main()
