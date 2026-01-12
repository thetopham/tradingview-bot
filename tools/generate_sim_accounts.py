#!/usr/bin/env python3
"""
generate_sim_accounts.py

Generate a sim_accounts.json file suitable for BROKER_MODE=sim.

Usage:
  python tools/generate_sim_accounts.py --count 20 --prefix sim --start-id 900001 --sl-usd 30 --tp-usd 60 --out sim_accounts.json

This file can be referenced by:
  SIM_ACCOUNTS_FILE=./sim_accounts.json
"""

import argparse
import json
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, required=True, help="How many accounts to create")
    p.add_argument("--prefix", type=str, default="sim", help="Name prefix (default: sim)")
    p.add_argument("--start-id", type=int, default=900001, help="First numeric account id")
    p.add_argument("--start-balance", type=float, default=50000.0, help="Starting balance for new accounts")
    p.add_argument("--sl-usd", type=float, default=30.0, help="Stop loss per contract in USD")
    p.add_argument("--tp-usd", type=float, default=60.0, help="Take profit per contract in USD")
    p.add_argument("--fill-policy", type=str, default="worst", choices=["worst", "best"])
    p.add_argument("--out", type=str, default="sim_accounts.json", help="Output path")
    args = p.parse_args()

    data = {}
    for i in range(args.count):
        name = f"{args.prefix}{i+1:03d}".lower()
        data[name] = {
            "id": int(args.start_id + i),
            "balance": float(args.start_balance),
            "sl_usd": float(args.sl_usd),
            "tp_usd": float(args.tp_usd),
            "fill_policy": args.fill_policy,
        }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {len(data)} accounts to {out_path}")


if __name__ == "__main__":
    main()
