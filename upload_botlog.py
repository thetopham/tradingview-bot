from supabase import create_client
import datetime
import time
import glob
import os
import re
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Config
log_dir = '/tmp/'
log_base = 'tradingview_projectx_bot.log'
log_pattern = os.path.join(log_dir, log_base + '*')  # matches .log, .log.1, .log.2, etc.
BUCKET = 'botlogs'
DAYS_TO_KEEP = 7

now = time.time()
utcnow = datetime.datetime.utcnow()

print("\n--- Local log cleanup ---")
# 1. Local cleanup
for filepath in glob.glob(log_pattern):
    if filepath.endswith('.log'):
        continue  # Always keep the active file
    if os.path.getmtime(filepath) < now - DAYS_TO_KEEP * 86400:
        try:
            os.remove(filepath)
            print(f"Deleted old local log: {filepath}")
        except Exception as e:
            print(f"Failed to delete {filepath}: {e}")

print("\n--- Log upload to Supabase Storage ---")
# 2. Upload logs to Supabase Storage
for filepath in glob.glob(log_pattern):
    try:
        mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
        ts = mod_time.strftime("%Y-%m-%d-%H-%M-%S")
        suffix = 'active' if filepath.endswith('.log') else filepath.split('.log.')[-1]
        storage_path = f'{log_base}-{ts}-{suffix}.log'
        with open(filepath, 'rb') as f:
            res = supabase.storage.from_(BUCKET).upload(storage_path, f, {"content-type": "text/plain"})
            print(f"Uploaded {filepath} as {storage_path}: {res}")
    except Exception as e:
        print(f"Failed to upload {filepath}: {e}")

print("\n--- Supabase Storage cleanup ---")
# 3. Supabase Storage cleanup
# List all files in the bucket (limit=1000; increase if needed)
try:
    files = supabase.storage.from_(BUCKET).list('', {'limit': 1000})
    # RegEx to extract timestamp from filename
    pattern = re.compile(r'tradingview_projectx_bot\.log-(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})')
    cutoff = utcnow - datetime.timedelta(days=DAYS_TO_KEEP)

    for f in files:
        name = f['name']  # filename only
        match = pattern.search(name)
        if not match:
            continue
        # Parse timestamp
        try:
            ts = datetime.datetime.strptime(match.group(1), "%Y-%m-%d-%H-%M-%S")
        except Exception:
            continue
        if ts < cutoff:
            try:
                supabase.storage.from_(BUCKET).remove(name)
                print(f"Deleted old storage log: {name}")
            except Exception as e:
                print(f"Failed to delete from storage {name}: {e}")
except Exception as e:
    print(f"Failed to list or clean up storage files: {e}")

print("\n--- Log sync and cleanup complete ---")
