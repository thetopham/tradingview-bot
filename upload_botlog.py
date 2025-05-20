from supabase import create_client
import datetime
import time
import glob
import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

log_dir = '/tmp/'
log_base = 'tradingview_projectx_bot.log'
log_pattern = os.path.join(log_dir, log_base + '*')  # matches .log, .log.1, .log.2, etc.

# Number of days to keep logs
DAYS_TO_KEEP = 7
now = time.time()

# --- Clean up old logs ---
for filepath in glob.glob(log_pattern):
    # Skip active log file if you want to keep it always
    if filepath.endswith('.log'):
        continue
    if os.path.getmtime(filepath) < now - DAYS_TO_KEEP * 86400:
        try:
            os.remove(filepath)
            print(f"Deleted old log file: {filepath}")
        except Exception as e:
            print(f"Failed to delete {filepath}: {e}")

# --- Upload logs ---
for filepath in glob.glob(log_pattern):
    try:
        mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
        ts = mod_time.strftime("%Y-%m-%d-%H-%M-%S")
        suffix = 'active' if filepath.endswith('.log') else filepath.split('.log.')[-1]
        storage_path = f'botlogs/{log_base}-{ts}-{suffix}.log'
        with open(filepath, 'rb') as f:
            res = supabase.storage.from_('botlogs').upload(storage_path, f, {"content-type": "text/plain"})
            print(f"Uploaded {filepath} as {storage_path}: {res}")
    except Exception as e:
        print(f"Failed to upload {filepath}: {e}")
