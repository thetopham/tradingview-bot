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

# Merge on ai_decision_id
joined = df_dec.merge(df_res, on='ai_decision_id', suffixes=('_decision', '_result'))

# Save merged DataFrame to CSV for review if you like
joined.to_csv('joined_performance.csv', index=False)

# Print some basic stats:
print("Number of joined trades:", len(joined))
print("Total PnL:", joined['total_pnl'].sum())
print("Average PnL:", joined['total_pnl'].mean())
print("Win rate:", (joined['total_pnl'] > 0).mean())

# Group by strategy or account:
print("\nPNL by strategy:")
print(joined.groupby('strategy_decision')['total_pnl'].sum())

print("\nTrades by signal (BUY/SELL):")
print(joined['signal_decision'].value_counts())

# Add more analysis as needed...
