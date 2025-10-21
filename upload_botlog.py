from supabase import create_client
import datetime, time, glob, os, re, sys
from dotenv import load_dotenv

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    print("Missing SUPABASE_URL or SUPABASE_KEY in env"); sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Config derived from env so it can't drift ---
ACTIVE_LOG = os.getenv("LOG_FILE", "/tmp/tradingview_bot.log")
log_dir = os.path.dirname(ACTIVE_LOG) or "."
log_base = os.path.basename(ACTIVE_LOG)              # e.g. tradingview_bot.log
log_pattern = os.path.join(log_dir, log_base + "*")  # matches .log, .log.1, .log.2, etc.

BUCKET = "botlogs"
DAYS_TO_KEEP = int(os.getenv("BOTLOG_DAYS_TO_KEEP", "7"))
MIN_BYTES_TO_UPLOAD = int(os.getenv("BOTLOG_MIN_BYTES", "128"))  # skip tiny/empty files

now = time.time()
utcnow = datetime.datetime.utcnow()

print(f"\nACTIVE_LOG={ACTIVE_LOG}")
print(f"Glob pattern={log_pattern}")

# --- 1) Local cleanup (old rotated files) ---
print("\n--- Local log cleanup ---")
for filepath in glob.glob(log_pattern):
    if filepath.endswith(".log"):
        continue  # keep the active file
    try:
        mtime = os.path.getmtime(filepath)
        if mtime < now - DAYS_TO_KEEP * 86400:
            os.remove(filepath)
            print(f"Deleted old local log: {filepath}")
    except Exception as e:
        print(f"Failed to delete {filepath}: {e}")

# --- 2) Upload logs to Supabase Storage ---
print("\n--- Log upload to Supabase Storage ---")
uploaded = 0
for filepath in sorted(glob.glob(log_pattern)):
    try:
        size = os.path.getsize(filepath)
        print(f"Found {filepath} ({size} bytes)")
        if size < MIN_BYTES_TO_UPLOAD:
            print(f"Skip (too small): {filepath}")
            continue

        mod_time = datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
        ts = mod_time.strftime("%Y-%m-%d-%H-%M-%S")

        # suffix: "active" for .log, otherwise rotation index (e.g., "1","2",…)
        if filepath.endswith(".log"):
            suffix = "active"
        else:
            # handles ...log.1, ...log.2.gz etc.
            suffix = filepath.split(".log.", 1)[-1].replace("/", "_")

        storage_name = f"{log_base}-{ts}-{suffix}.log"
        with open(filepath, "rb") as f:
            # upsert=True so re-runs don't fail if same name
            res = supabase.storage.from_(BUCKET).upload(
                storage_name, f, {"content-type": "text/plain", "upsert": "true"}
            )
        print(f"Uploaded {filepath} → {storage_name}: {res}")
        uploaded += 1
    except Exception as e:
        print(f"Failed to upload {filepath}: {e}")

print(f"Uploaded {uploaded} file(s).")

# --- 3) Remote cleanup (older than DAYS_TO_KEEP) ---
print("\n--- Supabase Storage cleanup ---")
try:
    files = supabase.storage.from_(BUCKET).list("", {"limit": 1000})
    pattern = re.compile(rf"{re.escape(log_base)}-(\d{{4}}-\d{{2}}-\d{{2}}-\d{{2}}-\d{{2}}-\d{{2}})")
    cutoff = utcnow - datetime.timedelta(days=DAYS_TO_KEEP)

    to_delete = []
    for f in files:
        name = f["name"]
        m = pattern.search(name)
        if not m:
            continue
        try:
            ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d-%H-%M-%S")
        except Exception:
            continue
        if ts < cutoff:
            to_delete.append(name)

    if to_delete:
        # supabase-py remove expects a list
        supabase.storage.from_(BUCKET).remove(to_delete)
        for n in to_delete:
            print(f"Deleted old storage log: {n}")
    else:
        print("No remote files eligible for deletion.")
except Exception as e:
    print(f"Failed remote cleanup: {e}")

print("\n--- Log sync and cleanup complete ---")
