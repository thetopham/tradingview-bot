# reports/run_daily_reports.py
"""
Daily artifact generator:
- Runs session_report.py + tod_analysis.py and captures stdout/stderr to disk
- Pulls latest TradingView chart screenshot URLs from Supabase (ai_trading_log.urls jsonb STRING)
- Downloads 5m/15m/30m screenshots into reports/daily/YYYY-MM-DD/charts/
- Writes summary.json + daily_report.md
- Zips the folder into bundle.zip

Expected layout:
  tradingview-bot/
    .env
    config.py
    reports/
      run_daily_reports.py   (this file)
      session_report.py
      tod_analysis.py
"""

import os
import sys
import json
import zipfile
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, time as dtime

import requests

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None


def _project_paths():
    reports_dir = Path(__file__).resolve().parent          # .../tradingview-bot/reports
    project_root = reports_dir.parent                      # .../tradingview-bot
    env_path = project_root / ".env"
    return project_root, reports_dir, env_path


def _load_env(env_path: Path):
    # Load .env deterministically (works under systemd)
    if load_dotenv and env_path.exists():
        load_dotenv(dotenv_path=env_path)


def _pick_python(project_root: Path) -> str:
    """
    Prefer repo venvs:
      ./venv/bin/python
      ./.venv/bin/python
    fallback: sys.executable
    """
    candidates = [
        project_root / "venv" / "bin" / "python",
        project_root / "venv" / "bin" / "python3",
        project_root / ".venv" / "bin" / "python",
        project_root / ".venv" / "bin" / "python3",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def run_and_capture(py: str, cwd: Path, script_path: Path, out_txt: Path):
    """
    Run a python script and save combined stdout/stderr to out_txt.
    Ensures cwd is project root and PYTHONPATH includes project root.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(cwd) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    p = subprocess.run(
        [py, str(script_path)],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
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


def _now_local(tz_name: str):
    if ZoneInfo is None:
        return datetime.now()
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now()


def _session_start(now_local: datetime, session_start_hour: int = 16) -> datetime:
    """
    Session start at 16:00 local time.
    If it's before 16:00 now, use yesterday 16:00.
    """
    start_today = now_local.replace(hour=session_start_hour, minute=0, second=0, microsecond=0)
    if now_local < start_today:
        return start_today - timedelta(days=1)
    return start_today


def _to_utc_iso(dt_local: datetime) -> str:
    # If tz-aware, convert; if naive, assume it's already UTC-ish.
    if dt_local.tzinfo is None:
        return dt_local.isoformat() + "Z"
    return dt_local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")


def _normalize_supabase_url(url: str) -> str:
    url = (url or "").strip()
    if url and not url.endswith("/"):
        url += "/"
    return url


def fetch_latest_chart_urls_from_supabase(session_start_utc_iso: str, limit: int = 2000):
    """
    ai_trading_log.urls is jsonb STRING (a single quoted URL), e.g.
      "https://storage.googleapis.com/.../5m.jpg"
    So we fetch recent rows and scan for /5m.jpg /15m.jpg /30m.jpg.
    """
    supabase_url = _normalize_supabase_url(os.getenv("SUPABASE_URL"))
    supabase_key = (os.getenv("SUPABASE_KEY") or "").strip()

    if not supabase_url or not supabase_key:
        return {
            "ok": False,
            "error": "Missing SUPABASE_URL or SUPABASE_KEY in .env",
            "urls": {},
        }

    # PostgREST endpoint
    endpoint = supabase_url.rstrip("/") + "/rest/v1/ai_trading_log"

    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Accept": "application/json",
    }

    params = {
        "select": "timestamp,account,urls",
        "timestamp": f"gte.{session_start_utc_iso}",
        "order": "timestamp.desc",
        "limit": str(limit),
    }

    try:
        r = requests.get(endpoint, headers=headers, params=params, timeout=30)
        if r.status_code >= 300:
            return {
                "ok": False,
                "error": f"Supabase query failed: {r.status_code} {r.text[:200]}",
                "urls": {},
            }
        rows = r.json()
    except Exception as exc:
        return {"ok": False, "error": f"Supabase request error: {exc}", "urls": {}}

    # Find first URL per timeframe (rows are newest-first)
    found = {"5m": None, "15m": None, "30m": None}
    found_meta = {"5m": None, "15m": None, "30m": None}

    for row in rows:
        u = row.get("urls")
        if not u:
            continue

        # urls is jsonb string -> python may deserialize as str already
        if isinstance(u, str):
            url = u
        else:
            # if it somehow comes back as json, stringify it
            url = str(u)

        url = url.strip().strip('"')
        url = url.replace("/n", "")  # cleanup old artifact if present

        for tf in ("5m", "15m", "30m"):
            if found[tf] is None and f"/{tf}.jpg" in url:
                found[tf] = url
                found_meta[tf] = {
                    "timestamp": row.get("timestamp"),
                    "account": row.get("account"),
                }

        if all(found.values()):
            break

    urls_out = {k: v for k, v in found.items() if v}
    return {"ok": True, "error": None, "urls": urls_out, "meta": found_meta}


def download_image(url: str, dest_path: Path) -> bool:
    try:
        r = requests.get(url, timeout=30)
        if r.status_code >= 300:
            return False
        dest_path.write_bytes(r.content)
        return True
    except Exception:
        return False


def main():
    project_root, reports_dir, env_path = _project_paths()
    _load_env(env_path)

    # Use MT by default (America/Denver is safest for MT w/ DST)
    tz_name = os.getenv("REPORT_TZ", "America/Denver")
    now_local = _now_local(tz_name)
    session_start_local = _session_start(now_local, session_start_hour=16)

    # Label the report by the session start date (so the “market day” aligns)
    day_label = session_start_local.strftime("%Y-%m-%d")
    session_start_utc_iso = _to_utc_iso(session_start_local)

    py = _pick_python(project_root)

    outdir = reports_dir / "daily" / day_label
    charts_dir = outdir / "charts"
    outdir.mkdir(parents=True, exist_ok=True)
    charts_dir.mkdir(parents=True, exist_ok=True)

    # Run your existing scripts, capture output exactly as you see it
    # (cwd=project_root so imports like config.py work)
    run_and_capture(py, project_root, reports_dir / "session_report.py", outdir / "session_report.txt")
    run_and_capture(py, project_root, reports_dir / "tod_analysis.py", outdir / "tod_analysis.txt")

    # Pull + download screenshots
    chart_result = fetch_latest_chart_urls_from_supabase(session_start_utc_iso)
    downloaded = {}

    if chart_result.get("ok") and chart_result.get("urls"):
        for tf, url in chart_result["urls"].items():
            fname = f"{tf}.jpg"
            ok = download_image(url, charts_dir / fname)
            downloaded[tf] = ok

    # Build markdown with embedded local images if downloaded, else use external URL
    chart_md_lines = ["## Charts"]
    for tf in ("5m", "15m", "30m"):
        local_path = f"charts/{tf}.jpg"
        url = (chart_result.get("urls") or {}).get(tf)

        chart_md_lines.append(f"### {tf}")
        if downloaded.get(tf):
            chart_md_lines.append(f"![{tf} chart]({local_path})")
        elif url:
            chart_md_lines.append(f"![{tf} chart]({url})")
        else:
            chart_md_lines.append(f"_No {tf} screenshot found after session start._")
        chart_md_lines.append("")

    daily_md = f"""# Daily Trading Report — {day_label}

**Session start (local):** {session_start_local.isoformat()}
**Session start (UTC):** {session_start_utc_iso}

## Session report
See `session_report.txt`

## Time-of-day analysis
See `tod_analysis.txt`

{chr(10).join(chart_md_lines)}
"""
    (outdir / "daily_report.md").write_text(daily_md, encoding="utf-8")

    summary = {
        "day": day_label,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "report_tz": tz_name,
        "session_start_local": session_start_local.isoformat(),
        "session_start_utc": session_start_utc_iso,
        "files": ["session_report.txt", "tod_analysis.txt", "daily_report.md", "summary.json", "bundle.zip"],
        "charts": {
            "found_urls": chart_result.get("urls") if chart_result else {},
            "downloaded": downloaded,
            "error": chart_result.get("error") if chart_result else None,
            "meta": chart_result.get("meta") if chart_result else None,
        },
    }
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    zip_path = outdir / "bundle.zip"
    zip_dir(outdir, zip_path)

    print(f"[OK] Wrote daily report to: {outdir}")
    print(f"[OK] Bundle: {zip_path}")


if __name__ == "__main__":
    main()
