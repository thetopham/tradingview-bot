# Repository Guidelines

## Project Structure & Module Organization
- `tradingview_projectx_bot.py` is the Flask entry point (webhook handling, AI routing, strategy dispatch).
- Core modules: `api.py`, `auth.py`, `position_manager.py`, `strategies.py`, `signalr_listener.py`, `scheduler.py`, `market_regime.py`.
- `templates/` contains dashboard/report HTML; `reports/` contains report runners; `scripts/` holds maintenance utilities.
- `n8n/` contains exported workflow JSON; `documentation/` is the canonical system docs.
- `requirements.txt` and `env.example` capture dependencies and required config.

## Build, Test, and Development Commands
- `python -m venv venv && source venv/bin/activate` sets up the local virtualenv.
- `pip install -r requirements.txt` installs Python dependencies.
- `python tradingview_projectx_bot.py` runs the webhook server and dashboard locally.
- `curl http://localhost:$TV_PORT/healthz` is a quick health check when the server is running.
- Report scripts are run ad-hoc, e.g. `python reports/run_daily_reports.py`.

## Coding Style & Naming Conventions
- Python code uses 4-space indentation and snake_case for functions/vars; classes use CapWords; constants are UPPER_SNAKE.
- Keep configuration in `config.py` and environment variables from `.env`; avoid hardcoding secrets.
- No formatter/linter is enforced; keep changes minimal and consistent with nearby code.

## Testing Guidelines
- There is no automated test suite in this repo today.
- For changes to execution logic, manually verify webhook handling, logging, and the dashboard feed; capture any new verification steps in the PR.

## Commit & Pull Request Guidelines
- Commit messages are short, imperative, and capitalized (e.g., “Fix …”, “Update …”, “Implement …”).
- PRs should describe behavior changes and risk impact, link any related issue, and include screenshots for `templates/` changes.
- Call out required env/config updates (`env.example`, `config.py`) in the PR description.

## Security & Operational Safety
- Treat `.env` as sensitive; never commit credentials or webhook secrets.
- This bot can execute real orders—validate changes on a sim account before live use.
