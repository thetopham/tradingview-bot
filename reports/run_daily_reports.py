# reports/run_daily_reports.py
import sys, json, zipfile, subprocess
from pathlib import Path
from datetime import datetime

def run_and_capture(py: str, script_path: Path, out_txt: Path):
    p = subprocess.run(
        [py, str(script_path)],
        capture_output=True,
        text=True,
    )
    out_txt.write_text(
        (p.stdout or "") + ("\n\nSTDERR:\n" + p.stderr if p.stderr else ""),
        encoding="utf-8",
    )
    if p.returncode != 0:
        raise RuntimeError(f"{script_path.name} failed (rc={p.returncode})")

def zip_dir(src_dir: Path, zip_path: Path):
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in src_dir.rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(src_dir))

def main():
    day = datetime.now().strftime("%Y-%m-%d")

    reports_dir = Path(__file__).resolve().parent              # .../tradingview-bot/reports
    project_root = reports_dir.parent                           # .../tradingview-bot
    venv_py = project_root / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else sys.executable

    outdir = reports_dir / "daily" / day
    charts_dir = outdir / "charts"
    outdir.mkdir(parents=True, exist_ok=True)
    charts_dir.mkdir(parents=True, exist_ok=True)

    # Capture your two analyses exactly as they print today
    run_and_capture(py, reports_dir / "session_report.py", outdir / "session_report.txt")
    run_and_capture(py, reports_dir / "tod_analysis.py", outdir / "tod_analysis.txt")

    summary = {
        "day": day,
        "generated_at": datetime.now().isoformat(),
        "files": ["session_report.txt", "tod_analysis.txt", "daily_report.md", "bundle.zip"],
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
