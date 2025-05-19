from supabase import create_client
from dotenv import load_dotenv
import os
import pandas as pd

# Load .env for SUPABASE_URL and SUPABASE_KEY
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Fetch all AI decisions and trade results
decisions = supabase.table('ai_trading_log').select("*").execute().data
results = supabase.table('trade_results').select("*").execute().data

# Convert to pandas DataFrames
df_dec = pd.DataFrame(decisions)
df_res = pd.DataFrame(results)

print("ai_trading_log columns:", df_dec.columns.tolist())
print("trade_results columns:", df_res.columns.tolist())

# If ai_decision_id is missing but another similar key exists, rename it
for df, name in [(df_dec, "ai_trading_log"), (df_res, "trade_results")]:
    if 'ai_decision_id' not in df.columns:
        alt_keys = [col for col in df.columns if col.lower() in {'aidecisionid', 'ai_decisionid', 'decision_id', 'id'}]
        if alt_keys:
            print(f"Renaming {alt_keys[0]} to ai_decision_id in {name}")
            df.rename(columns={alt_keys[0]: 'ai_decision_id'}, inplace=True)

if 'ai_decision_id' not in df_dec.columns or 'ai_decision_id' not in df_res.columns:
    print("ERROR: 'ai_decision_id' not found in both DataFrames. Please check your Supabase tables.")
    exit(1)

df_dec['ai_decision_id'] = df_dec['ai_decision_id'].astype(str)
df_res['ai_decision_id'] = df_res['ai_decision_id'].astype(str)

# Merge on ai_decision_id
joined = df_dec.merge(df_res, on='ai_decision_id', suffixes=('_decision', '_result'))

# Save merged DataFrame to CSV for review if you like
joined.to_csv('joined_performance.csv', index=False)

# Print some basic stats:
print("Number of joined trades:", len(joined))
if len(joined) == 0:
    print("No joined trades found. Check your data and 'ai_decision_id' linkage.")
    exit(0)

print("Total PnL:", joined['total_pnl'].sum())
print("Average PnL:", joined['total_pnl'].mean())
print("Win rate:", (joined['total_pnl'] > 0).mean())

# Group by account (AI strategy), and print stats for each account
if 'account_decision' in joined.columns:
    accounts = joined['account_decision'].unique()
    print("\nAccounts found:", accounts)
    for acct in accounts:
        print(f"\n=== Performance for account '{acct}' ===")
        acct_df = joined[joined['account_decision'] == acct]
        print("  Number of trades:", len(acct_df))
        print("  Total PnL:", acct_df['total_pnl'].sum())
        print("  Average PnL:", acct_df['total_pnl'].mean())
        print("  Win rate:", (acct_df['total_pnl'] > 0).mean())
        if 'strategy_decision' in acct_df.columns:
            print("  PnL by strategy:")
            print(acct_df.groupby('strategy_decision')['total_pnl'].sum())
        if 'signal_decision' in acct_df.columns:
            print("  Trades by signal (BUY/SELL):")
            print(acct_df['signal_decision'].value_counts())

# Optionally: show global PnL by account and strategy
if 'account_decision' in joined.columns and 'strategy_decision' in joined.columns:
    print("\n=== Global PnL by Account and Strategy ===")
    print(joined.groupby(['account_decision', 'strategy_decision'])['total_pnl'].sum())

# If you want, also save per-account CSVs for further review
    for acct in accounts:
        acct_df = joined[joined['account_decision'] == acct]
        acct_df.to_csv(f'performance_{acct}.csv', index=False)
