# session_report.py
import requests
import pandas as pd
from zoneinfo import ZoneInfo
from datetime import datetime, time, timedelta

TV_PORT = 5000
BASE_URL = f"http://localhost:{TV_PORT}"

TZ_LOCAL = ZoneInfo("America/Denver")
OPEN_T = time(16, 0)   # 4pm MT
CLOSE_T = time(14, 0)  # 2pm MT

# Keep your same buckets (in MT). You can tweak later.
SESSIONS = [
    ("Asia",         time(16, 0), time(21, 0)),  # 4pm–9pm MT
    ("Late US",      time(21, 0), time( 1, 0)),  # 9pm–1am
    ("London",       time( 1, 0), time( 6, 0)),  # 1am–6am
    ("Pre-NY",       time( 6, 0), time( 7,30)),  # 6am–7:30am
    ("NY Open",      time( 7,30), time(10, 0)),  # 7:30am–10am
    ("NY Midday",    time(10, 0), time(12, 0)),  # 10am–12pm
    ("NY Afternoon", time(12, 0), time(14, 0)),  # 12pm–2pm
]

def in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return (t >= start) or (t < end)

def label_session(dt_local: pd.Timestamp) -> str:
    t = dt_local.time()
    for name, start, end in SESSIONS:
        if in_window(t, start, end):
            return name
    return "Other"

def profit_factor(pnls: pd.Series):
    wins = pnls[pnls > 0].sum()
    losses = -pnls[pnls < 0].sum()
    if losses == 0:
        return None
    return float(wins / losses)

def trading_day_window(now_local: datetime):
    now_t = now_local.timetz().replace(tzinfo=None)

    if now_t >= OPEN_T:
        start = now_local.replace(hour=OPEN_T.hour, minute=0, second=0, microsecond=0)
        session_close = (start + timedelta(days=1)).replace(hour=CLOSE_T.hour, minute=0, second=0, microsecond=0)
    else:
        start = (now_local - timedelta(days=1)).replace(hour=OPEN_T.hour, minute=0, second=0, microsecond=0)
        session_close = now_local.replace(hour=CLOSE_T.hour, minute=0, second=0, microsecond=0)

    if CLOSE_T <= now_t < OPEN_T:
        end = session_close
    else:
        end = min(now_local, session_close)

    return start, end

def main():
    now_local = datetime.now(TZ_LOCAL)
    start_local, end_local = trading_day_window(now_local)

    data = requests.get(
        f"{BASE_URL}/dashboard/data",
        params={"account":"all","range":"7d","include_open":"false"},
        timeout=20
    ).json()

    df = pd.DataFrame(data.get("rows", []))
    if df.empty:
        print("No rows returned.")
        return

    # Attribute realized PnL to exit_time (fallback decision_time)
    ts = df["exit_time"].fillna(df["decision_time"])
    dt = pd.to_datetime(ts, errors="coerce", utc=True).dt.tz_convert(TZ_LOCAL)

    df["dt_local"] = dt
    df["pnl"] = pd.to_numeric(df["pnl"], errors="coerce")
    df["win"] = df["pnl"] > 0

    # Filter to 4pm → 2pm window
    df = df[(df["dt_local"] >= start_local) & (df["dt_local"] <= end_local)].copy()

    if df.empty:
        print(f"No rows considered between {start_local} and {end_local}.")
        return

    df["session"] = df["dt_local"].apply(label_session)

    grp = df.groupby(["account","session"])
    summary = grp["pnl"].agg(trades="count", net_pnl="sum", avg="mean").reset_index()
    summary["win_rate"] = grp["win"].mean().values
    summary["pf"] = grp["pnl"].apply(profit_factor).values

    session_order = [s[0] for s in SESSIONS] + ["Other"]
    summary["session"] = pd.Categorical(summary["session"], categories=session_order, ordered=True)
    summary = summary.sort_values(["account","session"])

    print(f"\nWindow: {start_local}  →  {end_local}\n")

    print("=== PNL BY SESSION (per account) ===")
    print(summary.to_string(index=False))

    net_by_session = summary.pivot_table(index="account", columns="session", values="net_pnl", aggfunc="sum").fillna(0.0)
    net_by_session = net_by_session.reindex(columns=session_order, fill_value=0.0)
    cum = net_by_session.cumsum(axis=1)

    print("\n=== NET PNL BY SESSION (wide) ===")
    print(net_by_session.round(2).to_string())

    print("\n=== CUMULATIVE PNL BY SESSION ORDER (shows give-back) ===")
    print(cum.round(2).to_string())

if __name__ == "__main__":
    main()
