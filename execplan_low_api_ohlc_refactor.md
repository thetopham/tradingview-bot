# Simplify AI pipeline and bracket execution to cut API load

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. This document must be maintained in accordance with `PLANS.md` in the repository root.

## Purpose / Big Picture

We need to replace the current image-driven, polling-heavy trading flow with a lean OHLC-driven decision pipeline and a single server-side bracket order per trade. The goal is to minimize Topstep/n8n/Supabase calls while keeping the multi-timeframe context (5m/15m/30m plus 1h/4h/1D) and letting the AI only choose high-level actions (buy/sell/hold, enter/exit/add/reduce). After this change the bot should gather OHLC arrays, ask the AI for a simple action + bracket template choice, and submit one bracket order without client-side SL/TP micromanagement or screenshot analysis.

## Progress

- [x] (2025-02-07 00:00Z) Drafted initial ExecPlan describing the reduced-API, OHLC-only pipeline.
- [x] (2025-02-07 00:45Z) Implemented code changes: OHLC-only analysis, bracket template config, unified bracket executor, webhook simplification.
- [ ] Validate behavior and document outcomes.

## Surprises & Discoveries

- None yet.

## Decision Log

- Decision: Use a single Topstep bracket order endpoint with a template name from config (per-account override where present) instead of client-side SL/TP management.
  Rationale: Aligns with the requirement to stop micromanaging protective orders and reduce order-related API chatter.
  Date/Author: 2025-02-07 / assistant

- Decision: Remove screenshot/chart snapshot ingestion from `fetch_multi_timeframe_analysis` and rely exclusively on tv_datafeed OHLC aggregation for AI inputs while leaving dashboard image URLs untouched elsewhere.
  Rationale: User requested removal of vision-based analysis; OHLC provides cleaner signals and fewer external calls.
  Date/Author: 2025-02-07 / assistant

- Decision: Normalize AI output to `action`, `direction`, `size`, and `bracket` fields so the webhook handler can route uniformly without strategy-specific branches.
  Rationale: Simplifies the dispatch layer and removes `run_bracket`/`run_brackmod`/`run_pivot` complexity.
  Date/Author: 2025-02-07 / assistant

## Outcomes & Retrospective

- Pending implementation.

## Context and Orientation

Key modules:
- `api.py` wraps ProjectX/Topstep, Supabase, and AI calls. It now builds regime analyses from OHLC-only data and exposes a bracket placement helper.
- `strategies.py` holds the unified `execute_bracket_decision` flow that submits a single server-side bracket order without client-managed stops/targets.
- `tradingview_projectx_bot.py` is the Flask entrypoint that handles webhooks, calls the AI (`ai_trade_decision_with_regime`), and dispatches to the unified strategy.
- `config.py`/`env.example` define environment variables including bracket template settings and OHLC timeframe options.

## Plan of Work

Describe the sequence of edits to move toward the low-API architecture:

1. **Config wiring for bracket templates and timeframes**
   - In `config.py` add `BRACKET_TEMPLATE` (default) and per-account overrides via `BRACKET_TEMPLATE_<ACCOUNT>`. Add `AI_TIMEFRAMES` to control which OHLC intervals to assemble (default 5m/15m/30m/1h/4h/1D). Mirror these in `env.example` with comments.

2. **Topstep bracket helper**
   - In `api.py` add `place_bracket_order(acct_id, cid, side, size, template)` that issues a single bracket order payload (using `_post_with_retry`). Log the template used. Keep existing order helpers for compatibility but migrate strategy calls to this helper.

3. **OHLC-only regime/analysis pipeline**
   - Rewrite `fetch_multi_timeframe_analysis` to drop screenshot/chart fetches and n8n image fallbacks. It should pull 1m data from `tv_datafeed`, aggregate into configured timeframes (at least 5m/15m/30m/1h/4h/1D), and return an OHLC-focused snapshot for the AI (arrays of o/h/l/c/indicators). Update caching logic to reflect the new payload and remove chart URL dependencies.
   - Simplify `ai_trade_decision_with_regime` to send only OHLC arrays, regime summary, and position context; strip chart URL and screenshot references. Normalize the AI response into high-level fields (`action`, `direction`, `size`, `bracket`, `reason`, `ai_decision_id`).

4. **Decision pipeline consolidation**
   - Replace `run_bracket`/`run_brackmod`/`run_pivot` in `strategies.py` with a single `execute_bracket_decision` that: resolves the contract, checks for existing positions once, optionally flattens opposing positions, and submits one bracket order via `place_bracket_order`. Avoid polling `search_trades`/`search_open` loops and remove client-managed SL/TP logic. Track trades with minimal metadata (order id, ai_decision_id, action, bracket template).
   - Update `tradingview_projectx_bot.py` webhook handling to interpret AI decisions using the normalized fields. Map `action` values to behaviors: `enter` (if flat or aligned), `add`/`reduce` (adjust size via bracket or flatten), `exit` (flatten), `hold` (no order). Remove strategy string dispatch and default to the consolidated bracket executor.

5. **Cleanup and documentation**
   - Remove unused imports and legacy references to screenshot/chart analysis in affected modules. Optionally update `README.md` to note the OHLC-only AI inputs and server-side brackets.

## Concrete Steps

- Edit `config.py` and `env.example` to add bracket template settings and AI timeframe list; ensure defaults are sensible.
- Implement `place_bracket_order` in `api.py` and remove redundant stop/TP helpers from new flow while leaving older functions intact for other modules.
- Refactor `fetch_multi_timeframe_analysis` and `ai_trade_decision_with_regime` in `api.py` per the OHLC-only approach; verify return shapes used downstream.
- Replace strategy functions in `strategies.py` with a unified bracket executor; remove polling and client-side stop/TP placement.
- Simplify webhook dispatch in `tradingview_projectx_bot.py` to consume normalized AI decisions and call the unified strategy.
- Run lint/basic execution sanity checks if available (e.g., `python -m py_compile` or a short `python tradingview_projectx_bot.py --help` if it exists).

## Validation and Acceptance

- With environment variables set (including `BRACKET_TEMPLATE`), start the Flask app (`python tradingview_projectx_bot.py`) and post a minimal webhook payload (e.g., BUY alert) to confirm it triggers the AI call and submits a single bracket order log without spawning stop/TP placements or search loops.
- Inspect logs to ensure the decision payloads show OHLC timeframe counts and that no screenshot/n8n image fetches occur.
- Verify that HOLD/FLAT decisions result in no bracket calls, and EXIT/REDUCE decisions lead to a flatten/size reduction without repeated polling.

## Idempotence and Recovery

- Config changes are additive and safe to reapply. The unified strategy avoids persistent state; retries simply resubmit a bracket order. If Supabase/Topstep calls fail, logs should show the error; rerunning the webhook after fixing credentials should succeed. No destructive migrations are performed.

## Artifacts and Notes

- Keep snippets of log output showing an OHLC-only AI request, a normalized AI response (`action`, `direction`, `bracket`), and a single `place_bracket_order` call. Capture any Supabase aggregation counts to demonstrate data sufficiency.

## Interfaces and Dependencies

- New helper: in `api.py` define `place_bracket_order(acct_id: int, cid: str, side: int, size: int, bracket_template: str) -> dict` using `_post_with_retry` and Topstep’s bracket endpoint. Expected payload keys: `accountId`, `contractId`, `side`, `size`, `bracketTemplate` (string identifier).
- Unified strategy entry point: `strategies.execute_bracket_decision(acct_id: int, symbol: str, decision: dict, alert: str, ai_decision_id: Optional[str]) -> None` where `decision` contains `action`, `direction`, `size`, and `bracket`.
- AI decision shape: `ai_trade_decision_with_regime` returns `{action, direction, size, bracket, reason, ai_decision_id, regime, regime_confidence, position_context, ohlc_summary}` with `action` in {enter, add, reduce, exit, hold} and `direction` in {BUY, SELL, FLAT}.
