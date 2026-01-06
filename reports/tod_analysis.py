# tod_analysis.py
import requests
import pandas as pd
from zoneinfo import ZoneInfo
from datetime import datetime, time, timedelta

TV_PORT = 5000
BASE_URL = f"http://localhost:{TV_PORT}"
TZ = ZoneInfo("America/Denver")

OPEN_T = time(16, 0)  # 4:00pm MT
CLOSE_T = time(14, 0) # 2:00pm MT

def trading_day_window(now_local: datetime):
    """
    Returns (start_local, end_local) for the most recent trading session:
    4:00pm -> next day 2:00pm. End is capped at now unless we're in 2–4pm downtime.
    """
    now_t = now_local.timetz().replace(tzinfo=None)

    # Determine session start (most recent 4pm boundary)
    if now_t >= OPEN_T:
        start = now_local.replace(hour=OPEN_T.hour, minute=0, second=0, microsecond=0)
        session_close = (start + timedelta(days=1)).replace(hour=CLOSE_T.hour, minute=0, second=0, microsecond=0)
    else:
        start = (now_local - timedelta(days=1)).replace(hour=OPEN_T.hour, minute=0, second=0, microsecond=0)
        session_close = now_local.replace(hour=CLOSE_T.hour, minute=0, second=0, microsecond=0)

    # If we're between 2pm and 4pm, cap end at 2pm (session is done)
    if CLOSE_T <= now_t < OPEN_T:
        end = session_close
    else:
        end = min(now_local, session_close)

    return start, end

def main():
    now_local = datetime.now(TZ)
    start_local, end_local = trading_day_window(now_local)

    # Pull 7d then filter locally
    data = requests.get(
        f"{BASE_URL}/dashboard/data",
        params={"account": "all", "range": "7d", "include_open": "false"},
        timeout=20,
    ).json()

    df = pd.DataFrame(data.get("rows", []))
    if df.empty:
        print("No rows returned.")
        return

    # Use exit_time (realized PnL moment) for hour attribution; fallback to decision_time
    ts = df["exit_time"].fillna(df["decision_time"])
    dt = pd.to_datetime(ts, errors="coerce", utc=True).dt.tz_convert(TZ)

    df["dt_local"] = dt
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")

    # Filter to the trading-day window
    mask = (df["dt_local"] >= start_local) & (df["dt_local"] <= end_local)
    df = df[mask].copy()

    if df.empty:
        print(f"No rows considered between {start_local} and {end_local}.")
        return

    df["hour"] = df["dt_local"].dt.hour

    hourly = (df.groupby(["account", "hour"])["pnl"]
              .agg(trades="count", net="sum", avg="mean")
              .reset_index()
              .sort_values(["account", "hour"]))

    print(f"\nWindow: {start_local}  →  {end_local}\n")
    print(hourly.to_string(index=False))

if __name__ == "__main__":
    main()
