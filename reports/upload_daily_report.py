from supabase import create_client
from dotenv import load_dotenv
from pathlib import Path
import os

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BUCKET = os.getenv("SUPABASE_REPORTS_BUCKET", "daily-reports")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def upload_file(local_path: Path, storage_path: str, content_type: str):
    with open(local_path, "rb") as f:
        supabase.storage.from_(BUCKET).upload(
            storage_path,
            f,
            {"content-type": content_type}
        )

def main():
    # expects latest daily folder already created
    base = Path(__file__).resolve().parent / "daily"
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
