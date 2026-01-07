# reports/run_daily_reports.py
"""
Daily artifact generator:

- Runs report scripts and captures stdout/stderr to disk:
    - session_report.py        -> session_report.txt
    - tod_analysis.py          -> tod_analysis.txt
    - performance_attribution.py (optional) -> performance_attribution.txt
    - equity_drawdown_report.py (optional) -> equity_drawdown_report.txt
    - data_quality_report.py     (optional) -> data_quality_report.txt

- Pulls latest TradingView chart screenshot URLs from Supabase (ai_trading_log.urls jsonb STRING)
- Downloads 5m/15m/30m screenshots into reports/daily/YYYY-MM-DD/charts/
- Writes summary.json + daily_report.md
- Zips the folder into bundle.zip

Expected layout:
  tradingview-bot/
    .env
    reports/
      run_daily_reports.py   (this file)
      session_report.py
      tod_analysis.py
      performance_attribution.py      (optional)
      equity_drawdown_report.py       (optional)
      data_quality_report.py          (optional)
"""

import os
import sys
import json
import zipfile
import subprocess
from pathlib import Path
from datetime import datetime, timedelta

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


def run_and_capture(py: str, cwd: Path, script_path: Path, out_txt: Path, *, allow_fail: bool = True):
    """
    Run a python script and save combined stdout/stderr to out_txt.
    Ensures cwd is project root and PYTHONPATH includes project root.
    Returns a status dict with rc and any error message.
    """
    status = {
        "script": script_path.name,
        "path": str(script_path),
        "out_txt": str(out_txt),
        "ran": False,
        "rc": None,
        "ok": False,
        "error": None,
    }

    if not script_path.exists():
        msg = f"Script not found, skipping: {script_path}"
        out_txt.write_text(msg + "\n", encoding="utf-8")
        status.update({"ran": False, "rc": None, "ok": False, "error": msg})
        return status

    env = os.environ.copy()
    env["PYTHONPATH"] = str(cwd) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    p = subprocess.run(
        [py, str(script_path)],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    )

    status["ran"] = True
    status["rc"] = p.returncode
    status["ok"] = (p.returncode == 0)

    header = [
        f"script: {script_path.name}",
        f"rc: {p.returncode}",
        f"cwd: {cwd}",
        f"ts_utc: {datetime.utcnow().isoformat()}Z",
        "-" * 80,
        "",
    ]

    body = (p.stdout or "")
    err = (p.stderr or "")

    out_txt.write_text(
        "\n".join(header) + body + ("\n\nSTDERR:\n" + err if err else ""),
        encoding="utf-8",
    )

    if p.returncode != 0:
        status["error"] = f"{script_path.name} failed (rc={p.returncode})"
        if not allow_fail:
            raise RuntimeError(status["error"])

    return status


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
    try:
        return dt_local.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
    except Exception:
        return dt_local.isoformat() + "Z"


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


def _tail_text(path: Path, max_lines: int = 220) -> str:
    """
    Grab the last N lines for embedding into markdown.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines)
        return "\n".join(lines[-max_lines:])
    except Exception as exc:
        return f"(unable to read {path.name}: {exc})"


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

    # ----------------------------
    # Run report scripts (non-fatal)
    # ----------------------------
    script_specs = [
        ("session_report.py", "session_report.txt"),
        ("tod_analysis.py", "tod_analysis.txt"),
        # Optional “mature system” reports (only run if present):
        ("performance_attribution.py", "performance_attribution.txt"),
        ("equity_drawdown_report.py", "equity_drawdown_report.txt"),
        ("data_quality_report.py", "data_quality_report.txt"),
    ]

    script_statuses = []
    for script_name, out_name in script_specs:
        status = run_and_capture(
            py,
            project_root,
            reports_dir / script_name,
            outdir / out_name,
            allow_fail=True,  # don't break the daily pipeline
        )
        script_statuses.append(status)

    # ----------------------------
    # Pull + download screenshots
    # ----------------------------
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

    # ----------------------------
    # Build daily_report.md
    # ----------------------------
    md_parts = []
    md_parts.append(f"# Daily Trading Report — {day_label}\n")
    md_parts.append(f"**Session start (local):** {session_start_local.isoformat()}")
    md_parts.append(f"**Session start (UTC):** {session_start_utc_iso}")
    md_parts.append(f"**Report TZ:** {tz_name}")
    md_parts.append("")

    # Script status overview
    md_parts.append("## Report Script Status")
    md_parts.append("")
    for st in script_statuses:
        flag = "✅" if st.get("ok") else ("⚠️" if st.get("ran") else "⏭️")
        md_parts.append(f"- {flag} `{st['script']}` → `{Path(st['out_txt']).name}` (rc={st.get('rc')})")
    md_parts.append("")

    # Embed key report outputs
    for script_name, out_name in script_specs:
        out_path = outdir / out_name
        title = out_name.replace(".txt", "").replace("_", " ").title()
        md_parts.append(f"## {title}")
        md_parts.append(f"_See `{out_name}` for full output._\n")
        md_parts.append("```")
        md_parts.append(_tail_text(out_path, max_lines=220))
        md_parts.append("```")
        md_parts.append("")

    md_parts.append("\n".join(chart_md_lines))
    md_parts.append("")

    daily_md = "\n".join(md_parts)
    (outdir / "daily_report.md").write_text(daily_md, encoding="utf-8")

    # ----------------------------
    # summary.json + bundle.zip
    # ----------------------------
    summary = {
        "day": day_label,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "report_tz": tz_name,
        "session_start_local": session_start_local.isoformat(),
        "session_start_utc": session_start_utc_iso,
        "python": py,
        "scripts": script_statuses,
        "files": [
            "daily_report.md",
            "summary.json",
            "bundle.zip",
            # include all txt outputs that exist
        ] + [spec[1] for spec in script_specs if (outdir / spec[1]).exists()],
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
