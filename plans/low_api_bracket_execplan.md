# Simplify trading bot to server-side brackets and OHLC-driven AI

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. Maintain this document in accordance with PLANS.md at /workspace/tradingview-bot/PLANS.md.

## Purpose / Big Picture

We need to replace the current chatty, polling-heavy trading flow with a resilient pipeline that issues a single server-side bracket order per decision and relies on structured OHLC indicators instead of screenshots. After this change, the bot should (1) let AI pick only the trade action and bracket template, (2) submit one bracket request without local stop/take-profit micromanagement, and (3) build multi-timeframe OHLC datasets for AI input while avoiding screenshot fetches. The user-visible outcome is fewer external API calls and a deterministic order flow that mirrors Topstep's new bracket templates.

## Progress

- [x] (2025-02-18 00:00Z) Drafted ExecPlan outlining simplification goals and validation approach.
- [x] (2025-02-18 00:30Z) Added server-side bracket helper and unified strategy dispatch around simple brackets.
- [x] (2025-02-18 00:30Z) Removed polling-based strategies in favor of a single run_simple_bracket entry point and streamlined webhook handling.
- [x] (2025-02-18 00:30Z) Shifted AI decision payloads to OHLC-only inputs and removed screenshot/chart snapshot dependencies.
- [ ] Validate by dry-running webhook scenarios and confirming reduced API chatter in logs.

## Surprises & Discoveries

- None yet.

## Decision Log

- Decision: Store the plan under plans/low_api_bracket_execplan.md to isolate this refactor from prior docs.
  Rationale: Keeps the refactor-specific notes easy to locate without modifying existing root docs.
  Date/Author: 2025-02-18 / Assistant
- Decision: Default bracket selection now uses DEFAULT_BRACKET_TEMPLATE with optional per-account overrides, avoiding Python-side SL/TP geometry.
  Rationale: Aligns with Topstep server-side brackets and removes redundant order churn.
  Date/Author: 2025-02-18 / Assistant
- Decision: Removed n8n/screenshot fallbacks from regime analysis and rely solely on aggregated tv_datafeed OHLC data.
  Rationale: Cuts API calls and latency while keeping deterministic indicator inputs.
  Date/Author: 2025-02-18 / Assistant

## Outcomes & Retrospective

- Pending implementation.

## Context and Orientation

The trading webhook entrypoint is tradingview_projectx_bot.py, which delegates strategy execution to functions in strategies.py and interacts with ProjectX/Topstep via helpers in api.py. Current strategies run_bracket/run_brackmod/run_pivot place market orders, then poll search_open/search_trades while placing stops and take-profits locally. AI decisions are fetched through ai_trade_decision_with_regime in api.py, which blends OHLC data from Supabase tv_datafeed with chart snapshots from latest_chart_analysis. This refactor must centralize decision routing, remove polling loops, and rely on Topstep bracket templates via a single order submission function.

## Plan of Work

First, add a dedicated server-side bracket submission helper in api.py that accepts account ID, contract ID, side, size, and a bracket template identifier from config; it should rely on a single API call and avoid follow-up polling. Next, replace the existing run_bracket/run_brackmod/run_pivot flows in strategies.py with a simplified execution path that checks current exposure once, selects the template, and submits the unified bracket order; remove manual stop/TP placement, phantom sweeps, and trade polling. Update handle_webhook_logic in tradingview_projectx_bot.py to map AI outputs to the limited action set (buy/sell/hold/enter/exit/add/reduce/no position) and route all actionable signals through the new bracket path, rejecting unsupported extras. Finally, trim ai_trade_decision_with_regime in api.py to assemble OHLC-only context from tv_datafeed aggregation (5m/15m/30m/1h/4h/daily), drop screenshot/chart snapshot dependencies, and ensure the AI payload excludes SL/TP geometry, focusing solely on action and template selection. Documentation in the plan and code comments should clarify the new deterministic, low-call flow.

## Concrete Steps

1. In api.py, introduce a place_bracket_order helper that posts once to the ProjectX bracket endpoint (path /api/Order/placeBracket or equivalent) with accountId, contractId, side, size, and bracketTemplate. Read a default template map from config.py and allow overriding via webhook/AI payloads.
2. In strategies.py, replace run_bracket/run_brackmod/run_pivot with a single run_simple_bracket function that:
    - Resolves the contract via get_contract.
    - Checks for existing positions to avoid duplicate same-side entries or flatten opposite exposure using flatten_contract once.
    - Calls place_bracket_order without local stop/TP creation, polling, or phantom sweeps.
    - Records the trade via track_trade with minimal metadata (no stop/tp IDs).
3. Update tradingview_projectx_bot.py to normalize AI/alert inputs to the simplified action vocabulary and bracket template selection, dispatching only to run_simple_bracket for BUY/SELL/ENTER/EXIT/ADD/REDUCE signals and treating HOLD/NO_POSITION as no-ops.
4. In api.py's ai_trade_decision_with_regime, refactor market data gathering to aggregate OHLC from tv_datafeed (5m/15m/30m/1h/4h/1d), remove latest_chart_analysis/screenshot usage, and send the AI a compact payload containing OHLC arrays and indicator slices plus current position context—no screenshots or bracket geometry.
5. Update config.py/env.example to expose bracket template defaults and timeframe aggregation options if needed, keeping names consistent across modules.
6. Validate by running python -m tradingview_projectx_bot (or equivalent webhook replay) to ensure logs show a single bracket submission per trade and that AI payloads omit SL/TP fields; document observations in this plan and commit.

## Validation and Acceptance

The refactor is acceptable when: (1) webhook processing logs show a single placeBracket call per BUY/SELL signal with no subsequent stop/limit placements; (2) strategies.py no longer polls search_trades/search_open for stop/TP management; (3) ai_trade_decision_with_regime payloads include OHLC-derived features only and exclude screenshot URLs and stop/take-profit coordinates; and (4) manual webhook replay while the server runs produces no rate-limit warnings from repetitive polling.

## Idempotence and Recovery

The new helper and strategy are additive replacements; rerunning the server after edits should issue the same single-call behavior. If ProjectX rejects the bracket endpoint, fallback is to log and abort without placing partial orders. Removing screenshot dependencies is non-destructive and can be retried safely by clearing caches. Position flattening uses the existing flatten_contract guardrails to avoid orphaned orders.

## Artifacts and Notes

Include log snippets in this plan after validation that show a single bracket submission and absence of follow-up protective order placements. Note any API payload changes for future maintainers.

## Interfaces and Dependencies

- api.place_bracket_order(account_id: int, contract_id: str, side: int, size: int, template: str) -> dict
- strategies.run_simple_bracket(acct_id: int, sym: str, action: str, size: int, template: str | None, alert: str, ai_decision_id: Any)
- tradingview_projectx_bot.handle_webhook_logic adapts AI outputs to the run_simple_bracket interface and rejects unsupported instructions.
- ai_trade_decision_with_regime builds OHLC-only context from tv_datafeed without consulting latest_chart_analysis or screenshot artifacts.
