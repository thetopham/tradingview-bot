# Repository Guidelines

## Project Structure & Module Organization
- `tradingview_projectx_bot.py` exposes the Flask webhook entry point (`app`) and orchestrates market regime checks, AI decisions, and order routing.
- Supporting services live beside it: `api.py` wraps ProjectX, Supabase, and AI endpoints; `auth.py`, `state.py`, and `position_manager.py` guard session state; `scheduler.py` manages timed cleanups.
- Strategy logic is consolidated in `strategies.py`, while market-state helpers sit in the `market_regime*.py` trio. JSON files in the root define time-frame presets used by those helpers.
- Dashboard assets reside under `static/`; shared configuration is loaded from `config.py` and the `.env` template in `env.example`. Keep new data files small and name them after their interval (`15m.json`, etc.).

## Build, Test, and Development Commands
- Create an isolated environment before hacking: `python3 -m venv venv && source venv/bin/activate`.
- Install dependencies from `requirements.txt`: `pip install -r requirements.txt`.
- Run the webhook locally with Flask’s dev server: `python tradingview_projectx_bot.py`. The bot binds to `TV_PORT`; watch `logs/bot.log` for activity.
- Exercise the API without TradingView by replaying a payload: `curl -X POST http://localhost:5000/webhook -H "Content-Type: application/json" -d @sample.json`.
- For production-like checks, prefer `gunicorn tradingview_projectx_bot:app --bind 0.0.0.0:5000`.

## Coding Style & Naming Conventions
- Follow PEP 8 with 4-space indents. Keep modules focused; prefer functions over giant scripts.
- Use `snake_case` for functions, `CapWords` for classes, and uppercase with underscores for constants and environment keys.
- Keep loggers module-scoped (`logging.getLogger(__name__)`) and include context in every log message.
- When touching configuration, update `env.example` and `config.py` together to prevent drift.

## Testing Guidelines
- No formal test harness ships yet; validate changes with targeted webhook payloads and by observing Supabase/log output.
- Add lightweight smoke checks when possible (e.g., helper functions callable via `python -m module test`). If you introduce `pytest`, place tests under `tests/` and mirror module names.
- Document manual verification steps in the PR so others can replay them.

## Commit & Pull Request Guidelines
- Existing history leans on “Update <file>”; improve clarity with imperative summaries (`Add scheduler guard`, `Fix ProjectX auth retry`) and keep scope tight.
- Reference related alerts or logs and list any new environment variables in the description.
- Before opening a PR, confirm the bot boots, scheduled jobs run, and manual webhook tests pass. Include payload snippets, screenshots of dashboards if touched, and note Supabase or ProjectX impacts.
- Label breaking API changes loudly so downstream automation can adjust.
