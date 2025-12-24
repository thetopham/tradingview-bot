# Reduce API load with server-side brackets and OHLC-only AI

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This document must be maintained in accordance with PLANS.md at the repository root.

## Purpose / Big Picture

Move the trading bot away from heavy polling, screenshot-driven analysis, and Python-managed stop/TP trees. The goal is a lean pipeline where AI only picks the directional action and size, the bot submits a single server-side bracket template order, and market reasoning relies on multi-timeframe OHLC arrays (5m/15m/30m plus higher frames) instead of TradingView screenshots. Fewer API calls should make fills faster and reduce fragility in Topstep/Supabase/n8n.

## Progress

- [x] (2025-02-24 00:00Z) Collected requirements and repo context; drafted initial plan for bracket-only flow and OHLC-only analysis.
- [x] (2025-02-24 00:40Z) Implemented bracket-template executor, OHLC-only analysis pipeline, config/env updates, and README refresh.
- [ ] Validate behavior (lint/basic runtime check) and update documentation/Env vars.

## Surprises & Discoveries

- None yet.

## Decision Log

- Decision: Use a single bracket submission helper that references a server-defined template ID from config; retire Python-side TP/SL micromanagement.
  Rationale: Aligns with Topstep server brackets and eliminates polling loops, phantom checks, and manual TP/SL placement.
  Date/Author: 2025-02-24 / assistant

## Outcomes & Retrospective

Pending implementation.

## Context and Orientation

- Order orchestration lives in `tradingview_projectx_bot.py`, which calls strategy helpers in `strategies.py`.
- API wrappers are in `api.py`; current stop/TP placement relies on multiple `/api/Order` calls plus polling via `search_trades` and `search_open`.
- AI trade gating runs through `ai_trade_decision_with_regime` in `api.py`, which builds multi-timeframe context using Supabase OHLC plus TradingView chart snapshots and a fallback n8n image workflow.
- Risk/config values load from `config.py`; environment defaults sit in `env.example`.
- Trade tracking metadata is stored via `signalr_listener.track_trade` and logged to Supabase.

## Plan of Work

Describe the edit sequence and rationale:

1. Add a bracket-template order helper to `api.py` that posts a single bracket payload (account, contract, side, size, template ID, optional TIF/custom tag) and returns the order metadata. This replaces multiple stop/limit/phantom sweeps.
2. Simplify `strategies.py` into one entry point (e.g., `execute_bracket`) that resolves the contract, skips duplicate-direction entries, flattens opposite exposure once when needed, and submits the bracket template order. Remove `run_brackmod`/`run_pivot` and any polling helpers (`ensure_live_stop`, `_compute_entry_fill`, `check_for_phantom_orders`). Track trades with template metadata only.
3. Update `tradingview_projectx_bot.py` to route BUY/SELL through the new bracket executor, interpret AI responses in terms of simple actions (buy/sell/hold/flat/add/reduce), and drop strategy variants. Keep HOLD/FLAT behavior but avoid repeated `search_open` sweeps.
4. Refocus `fetch_multi_timeframe_analysis` in `api.py` to rely solely on OHLC arrays aggregated from `tv_datafeed` 1m bars. Expand default frames to include 1h/4h/1d, remove TradingView screenshot/chart snapshot pulls, and eliminate the n8n image fallback to cut API volume.
5. Extend `config.py` and `env.example` with bracket template configuration (default + optional per-account overrides) and any new timeframe settings needed by the OHLC pipeline. Keep README aligned with the simplified architecture and AI expectations.

## Concrete Steps

- Work from `/workspace/tradingview-bot`.
- Edit `api.py` to introduce `place_bracket_order` (single POST) and streamline `fetch_multi_timeframe_analysis` to OHLC-only aggregation (5m/15m/30m/1h/4h/1d). Remove chart snapshot and image fallback branches.
- Refactor `strategies.py` to a single bracket executor, removing polling loops and legacy strategy variants.
- Update `tradingview_projectx_bot.py` to use the new executor and simplified AI action handling; prune deprecated strategy cases.
- Align `config.py`, `env.example`, and README text with the new flow and configuration knobs.

## Validation and Acceptance

- Manual sanity check: run `python -m py_compile api.py strategies.py tradingview_projectx_bot.py` from the repo root to ensure syntax validity.
- Dry-run webhook logic by posting a minimal payload (no external calls) to confirm code paths load without errors.
- Acceptance: webhook logic dispatches BUY/SELL to `execute_bracket` without invoking stop/TP polling, and `fetch_multi_timeframe_analysis` returns OHLC-based analysis without referencing screenshots or n8n image fallbacks.

## Idempotence and Recovery

- Changes are configuration-driven (template IDs, timeframes). Re-running the steps keeps behavior consistent; fallback paths remain safe because HOLD/FLAT branches bail out early.
- If bracket submission fails, the code should log and return without partial TP/SL orders, so rerunning the alert is safe.

## Artifacts and Notes

- Capture key diffs in git commits; no binary assets required.

## Interfaces and Dependencies

- `api.place_bracket_order(account_id: int, contract_id: str, side: int, size: int, template: str, tif: str | None = None, tag: str | None = None) -> Dict` – posts one bracket order using the server-side template ID.
- `strategies.execute_bracket(acct_id: int, sym: str, sig: str, size: int, alert: str, ai_decision_id: Any = None, template: str | None = None)` – resolves contract and submits the bracket template order once.
