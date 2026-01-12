#!/usr/bin/env python3
"""Generate SIM_ACCOUNTS JSON for many accounts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Tuple


def _parse_number(value: str):
    if "." in value:
        return float(value)
    return int(value)


def parse_overrides(raw: str) -> Dict[str, Tuple[float, float]]:
    overrides: Dict[str, Tuple[float, float]] = {}
    if not raw:
        return overrides

    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError(f"Invalid override entry '{entry}', expected name=sl,tp")
        name, values = entry.split("=", 1)
        name = name.strip()
        if not name:
            raise ValueError(f"Invalid override entry '{entry}', missing name")
        if "," not in values:
            raise ValueError(f"Invalid override entry '{entry}', expected sl,tp")
        sl_raw, tp_raw = (part.strip() for part in values.split(",", 1))
        if not sl_raw or not tp_raw:
            raise ValueError(f"Invalid override entry '{entry}', expected sl,tp")
        overrides[name] = (_parse_number(sl_raw), _parse_number(tp_raw))

    return overrides


def build_accounts(
    count: int,
    prefix: str,
    id_start: int,
    balance: float,
    default_sl: float,
    default_tp: float,
    overrides: Dict[str, Tuple[float, float]],
) -> Dict[str, Dict[str, float]]:
    width = max(3, len(str(count)))
    accounts: Dict[str, Dict[str, float]] = {}

    for index in range(count):
        name = f"{prefix}{index + 1:0{width}d}"
        sl_value, tp_value = overrides.get(name, (default_sl, default_tp))
        accounts[name] = {
            "id": id_start + index,
            "balance": balance,
            "sl_usd": sl_value,
            "tp_usd": tp_value,
        }

    return accounts


def default_output_path() -> Path:
    return Path(__file__).resolve().parents[1] / "sim_accounts.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate SIM accounts JSON file.")
    parser.add_argument("--count", type=int, required=True, help="Number of accounts to generate")
    parser.add_argument("--prefix", default="sim", help="Account name prefix")
    parser.add_argument("--id-start", type=int, default=20001, help="Starting account id")
    parser.add_argument("--balance", type=_parse_number, default=50000, help="Account balance")
    parser.add_argument("--default-sl", type=_parse_number, default=30, help="Default stop loss USD")
    parser.add_argument("--default-tp", type=_parse_number, default=60, help="Default take profit USD")
    parser.add_argument(
        "--override",
        default="",
        help="Overrides in format name=sl,tp;name=sl,tp",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=default_output_path(),
        help="Output path for sim_accounts.json",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.count <= 0:
        raise SystemExit("--count must be greater than zero")
    overrides = parse_overrides(args.override)
    accounts = build_accounts(
        count=args.count,
        prefix=args.prefix,
        id_start=args.id_start,
        balance=args.balance,
        default_sl=args.default_sl,
        default_tp=args.default_tp,
        overrides=overrides,
    )
    output_path: Path = args.out
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(accounts, handle, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    main()
