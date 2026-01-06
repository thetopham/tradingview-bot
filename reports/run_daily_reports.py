# reports/run_daily_reports.py
import os, sys, json, zipfile, subprocess
from pathlib import Path
from datetime import datetime

def run_and_capture(script_path: Path, out_txt: Path):
    p = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True
    )
    out_txt.write_text(p.stdout + ("\n\nSTDERR:\n" + p.stderr if p.stderr else ""), encoding="utf-8")
    if p.returncode != 0:
        raise RuntimeError(f"{script_path.name} failed (rc={p.returncode})")

def zip_dir(src_dir: Path, zip_path: Path):
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in src_dir.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(src_dir))

def main():
    # local “market day” label; tweak if you prefer previous session date
    day = datetime.now().strftime("%Y-%m-%d")
    base = Path(__file__).resolve().parent
    base = Path(__file__).resolve().parent
    venv_py = base / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else sys.executable

    subprocess.run([py, str(run_script)], check=True)
    subprocess.run([py, str(upload_script)], check=True)
    outdir = base / "daily" / day
    charts_dir = outdir / "charts"
    outdir.mkdir(parents=True, exist_ok=True)
    charts_dir.mkdir(parents=True, exist_ok=True)

    # Run your existing scripts, capture output exactly as you see it
    run_and_capture(base / "session_report.py", outdir / "session_report.txt")
    run_and_capture(base / "tod_analysis.py", outdir / "tod_analysis.txt")

    summary = {
        "day": day,
        "generated_at": datetime.now().isoformat(),
        "files": ["session_report.txt", "tod_analysis.txt"]
    }
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    md = f"""# Daily Trading Report — {day}

## Session report
See `session_report.txt`

## Time-of-day analysis
See `tod_analysis.txt`
"""
    (outdir / "daily_report.md").write_text(md, encoding="utf-8")

    zip_path = outdir / "bundle.zip"
    zip_dir(outdir, zip_path)

    print(f"[OK] Wrote daily report to: {outdir}")
    print(f"[OK] Bundle: {zip_path}")

if __name__ == "__main__":
    main()
