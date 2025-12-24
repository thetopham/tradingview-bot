# Simplify trading pipeline with server-side brackets and OHLC-driven AI

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds. Maintain this document in accordance with PLANS.md (./PLANS.md) and keep it fully self-contained for future contributors.

## Purpose / Big Picture

The current bot hammers Topstep, n8n, and Supabase with polling loops, multi-leg order management, and screenshot workflows. This plan replaces that with a lean, deterministic pipeline that submits a single server-hosted bracket order per trade and relies on OHLC datasets instead of image analysis. After implementation, an operator can point TradingView alerts at the bot and observe that each alert triggers at most one Topstep order call using a predefined bracket template, with AI decisions driven solely by OHLC-aggregated indicators across multiple timeframes.

## Progress

- [x] (2025-02-03 00:00Z) Drafted ExecPlan describing goals and scope for server-side brackets and OHLC-only AI input.
- [x] (2025-02-03 00:45Z) Wired OHLC aggregation path as the only AI input and removed screenshot/GPT-Vision usage from the decision flow.
- [x] (2025-02-03 00:50Z) Replaced strategy functions with a single server-bracket execution path that avoids polling loops and local SL/TP management.
- [x] (2025-02-03 01:00Z) Simplified webhook decision flow so AI only chooses intent (buy/sell/hold/flat, enter/exit/add/reduce) plus bracket template selection.
- [ ] Update configuration/env docs and validate the new path end-to-end.

## Surprises & Discoveries

- None yet.

## Decision Log

- Decision: Use Topstep server-side bracket templates as the sole SL/TP mechanism and eliminate local stop/limit placement loops.
  Rationale: Reduces API calls and removes fragile polling; aligns with new Topstep capability noted by stakeholders.
  Date/Author: 2025-02-03 / GPT-5.1-Codex-Max

## Outcomes & Retrospective

(To be filled after implementation milestones. Summarize completed work and remaining gaps.)

## Context and Orientation

- Entry point: `tradingview_projectx_bot.py` handles TradingView webhooks, AI routing via `ai_trade_decision_with_regime`, and strategy dispatch.
- Execution helpers: `strategies.py` currently exposes `run_bracket`, `run_brackmod`, and `run_pivot`, each managing SL/TP locally with polling of `search_trades`/`search_open` from `api.py`.
- API wrapper: `api.py` wraps ProjectX/Topstep endpoints (market/limit/stop placement, search calls, Supabase logging) and hosts OHLC/chart aggregation helpers that currently mix screenshot data with tv_datafeed bars.
- Market regime: `market_regime_ohlc.py` provides OHLC-only regime analysis; `market_regime_hybrid.py` mixes chart snapshots with OHLC data.
- Configuration: `config.py` supplies account mappings, AI endpoints, and risk settings; `.env` keys currently lack bracket template identifiers.

## Plan of Work

Describe edits in sequence to keep changes incremental:

1. **Consolidate OHLC data pipeline**: Remove screenshot/vision dependencies from AI input. Update `api.py` data fetchers so `ai_trade_decision_with_regime` gathers only OHLC + indicator arrays (5m/15m/30m and optional 1h/4h/1D) using existing `tv_datafeed` Supabase tables and aggregation helpers. Ensure outputs are compact arrays suitable for GPT reasoning.
2. **Define bracket template configuration and API call**: Add config/env keys for a default Topstep bracket template (optionally per account). In `api.py`, add a thin `place_bracket_order` helper that sends a single ProjectX/Topstep request referencing the bracket template without local SL/TP math. Keep logging concise to avoid noisy retries.
3. **Replace strategy functions with a single deterministic path**: Rewrite `strategies.py` to expose one `execute_bracket_strategy` (name TBD) that: resolves the contract, checks for opposing positions once, flattens if necessary, and submits exactly one bracket order via the new API helper. Remove polling for fills, search_trades loops, and phantom order sweeps.
4. **Simplify webhook decision flow and AI contract**: Update `ai_trade_decision_with_regime` and webhook handling in `tradingview_projectx_bot.py` so AI only returns structured intents (buy/sell/hold + enter/exit/add/reduce, desired size, optional bracket template ID). Enforce allowed actions, coerce bracket template selection, and route everything through the new single strategy function. Maintain hold/flat handling but drop TP/SL instructions from AI payloads.
5. **Docs/config and validation**: Amend `env.example`/`README.md` with new bracket template variables and describe the OHLC-only pipeline. Add lightweight logging to confirm a single Topstep call per alert. Validate by running the Flask app locally and posting a sample webhook payload to ensure no polling or screenshot paths remain.

## Concrete Steps

- Working directory: `/workspace/tradingview-bot`.
- During implementation, run targeted commands (e.g., `python -m tradingview_projectx_bot` for sanity) and record outputs here with timestamps so the next contributor can compare expected behavior.

## Validation and Acceptance

Acceptance criteria:
- Posting a BUY/SELL webhook triggers exactly one Topstep order API call that references the configured bracket template; no stop/limit placement or trade-search polling occurs locally.
- AI payload construction uses OHLC arrays only; no screenshot/chart snapshot fetches or GPT-Vision references remain in the decision path.
- Webhook handling enforces AI actions within the allowed set (buy/sell/hold/flat and enter/exit/add/reduce) and logs the chosen bracket template.
- Configuration docs list required bracket template variables and multi-timeframe OHLC sources (5m/15m/30m/1h/4h/1D).

## Idempotence and Recovery

All changes are additive or replacements that can be re-run safely. If a new bracket call fails during testing, revert to the previous commit or disable the new strategy dispatch by setting the strategy to HOLD in the webhook payload. Env/config updates are backward-compatible when the bracket template is left unset (code should log a clear error and skip order placement).

## Artifacts and Notes

- Record any sample webhook payloads and log excerpts demonstrating single-call bracket execution once implemented.

## Interfaces and Dependencies

- New API helper signature (planned): `place_bracket_order(acct_id: int, cid: str, side: int, size: int, template_id: str, time_in_force: str = "DAY") -> dict` sending the minimal payload required by Topstep for server-side bracket templates.
- Strategy entry point (planned): `execute_bracket_strategy(acct_id: int, symbol: str, action: str, size: int, template_id: str, alert: str | None, ai_decision_id: str | int | None) -> None`.
- AI contract: `ai_trade_decision_with_regime` must return an object with fields `action` (BUY/SELL/HOLD/FLAT), `intent` (ENTER/EXIT/ADD/REDUCE), `size`, optional `bracket_template`, plus reasoning metadata; no SL/TP geometry allowed.

