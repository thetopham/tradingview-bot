import os
import requests
import datetime
import pytz
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
# --- TopstepX Config ---
PX_BASE         = os.getenv("PROJECTX_BASE_URL")
USER_NAME       = os.getenv("PROJECTX_USERNAME")
API_KEY         = os.getenv("PROJECTX_API_KEY")

# --- Supabase Config ---
SUPABASE_URL    = os.getenv("SUPABASE_URL")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

ACCOUNTS = {k[len("ACCOUNT_"):].lower(): int(v)
    for k, v in os.environ.items() if k.startswith("ACCOUNT_")}

# --- Authenticate to TopstepX ---
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

# --- Get all trade_results from Supabase in last X days ---
def get_logged_trade_ids(days=2):
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat()
    rows = supabase.table('trade_results').select("order_id,entry_time").gte('entry_time', cutoff).execute()
    seen = set()
    for r in rows.data:
        if r.get('order_id'):
            seen.add(str(r['order_id']))
    return seen

# --- Get all Topstep trades for an account in last X days ---
def get_recent_trades(acct_id, days=2):
    since = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).replace(tzinfo=pytz.UTC)
    resp = session.post(
        f"{PX_BASE}/api/Trade/search",
        json={"accountId": acct_id, "startTimestamp": since.isoformat()},
        timeout=(3.05, 10)
    )
    resp.raise_for_status()
    return resp.json().get('trades', [])

# --- Main: Find and upload missing trades ---
def main():
    days = 2
    logged = get_logged_trade_ids(days=days)
    print(f"Loaded {len(logged)} logged trades from Supabase.")

    for acct_name, acct_id in ACCOUNTS.items():
        print(f"\nChecking account: {acct_name} ({acct_id})")
        trades = get_recent_trades(acct_id, days=days)
        for trade in trades:
            order_id = str(trade.get('orderId') or "")
            contract_id = trade.get('contractId')
            # Only for MES for now
            if contract_id != "CON.F.US.MES.M25":
                continue
            if order_id in logged:
                continue  # Already logged
            # Only log completed (not open, not voided)
            if trade.get('voided', False):
                continue
            # Build payload for Supabase upload (mirror your normal trade_results schema)
            payload = {
                "ai_decision_id": None,   # Unknown if not in your ai log
                "order_id": order_id,
                "symbol": contract_id,
                "account": acct_id,
                "strategy": None,
                "signal": None,
                "entry_time": trade.get('entryTimestamp'),
                "exit_time": trade.get('exitTimestamp'),
                "duration_sec": trade.get('durationSec'),
                "size": trade.get('size'),
                "total_pnl": trade.get('profitAndLoss'),
                "alert": None,
                "raw_trades": [trade],
                "comment": "Catchup script: backfilled from TopstepX",
            }
            print(f"Uploading missing trade: {order_id}, pnl={trade.get('profitAndLoss')}, entry={trade.get('entryTimestamp')}")
            try:
                supabase.table('trade_results').insert(payload).execute()
                print("...Uploaded.")
            except Exception as e:
                print(f"...Upload failed: {e}")

if __name__ == "__main__":
    main()
