# reports/upload_daily_report.py
from supabase import create_client
from dotenv import load_dotenv
from pathlib import Path
import os

REPORTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = REPORTS_DIR.parent
ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(dotenv_path=ENV_PATH)

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").strip()
SUPABASE_KEY = (os.getenv("SUPABASE_KEY") or "").strip()
BUCKET = os.getenv("SUPABASE_REPORTS_BUCKET", "daily-reports")

if SUPABASE_URL and not SUPABASE_URL.endswith("/"):
    SUPABASE_URL += "/"

if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit("Missing SUPABASE_URL or SUPABASE_KEY (check .env).")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def upload_file(local_path: Path, storage_path: str, content_type: str):
    with open(local_path, "rb") as f:
        supabase.storage.from_(BUCKET).upload(
            storage_path,
            f,
            {"content-type": content_type},
        )

def main():
    base = REPORTS_DIR / "daily"
    day_folders = sorted([p for p in base.iterdir() if p.is_dir()])
    if not day_folders:
        raise SystemExit("No daily folders found.")
    day_dir = day_folders[-1]

    bundle = day_dir / "bundle.zip"
    summary = day_dir / "summary.json"

    day = day_dir.name
    upload_file(bundle, f"{day}/bundle.zip", "application/zip")
    upload_file(summary, f"{day}/summary.json", "application/json")

    print(f"[OK] Uploaded {day}/bundle.zip and summary.json to bucket {BUCKET}")

if __name__ == "__main__":
    main()
