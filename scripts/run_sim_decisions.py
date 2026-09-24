"""Run one eligible n8n decision per cached candle and simulated account."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run_pending(bot, now: datetime | None = None) -> list[tuple[str, str]]:
    from brokers.sim_decision_feed import latest_fresh

    if bot.BROKER_MODE != "sim" or bot.config["SIM_DECISION_SOURCE"] != "scheduler":
        raise RuntimeError("sim scheduler requires BROKER_MODE=sim and SIM_DECISION_SOURCE=scheduler")
    now = now or datetime.now(timezone.utc)
    adapter = bot.get_sim_adapter()
    done = []
    for snapshot in adapter.ledger.status():
        account = snapshot["account"]
        if not bot.AI_TEST_ENDPOINTS.get(account):
            continue
        bar = latest_fresh(bot.config["SIM_BROKER_DB"], snapshot["timeframe"], now)
        if not bar or adapter.processed_decision(account, bar["timestamp"]) is not None:
            continue
        result = bot.process_sim_webhook({"account": account, "bar": bar})
        done.append((account, result["decision"]["signal"]))
    return done


def main() -> int:
    import tradingview_projectx_bot as bot

    for account, signal in run_pending(bot):
        print(f"account={account} signal={signal}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
