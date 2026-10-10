# Numeric-only simulator operations

The Pi's simulator accepts **numeric market/account context only**. The five
numeric strategies remain `alpha`, `beta`, `gamma` (5m), `delta` (15m), and
`epsilon` (30m). The one-minute MES feed continues to simulate fills, brackets,
fees, and account risk. This does not enable ProjectX or any real broker order.

## Account policy

Set this in the private simulator environment file:

```text
BROKER_MODE=sim
SIM_DECISION_SOURCE=scheduler
SIM_ACTIVE_ACCOUNTS=alpha,beta,gamma,delta,epsilon
```

`SIM_ACTIVE_ACCOUNTS` is an explicit allowlist for model decisions and dashboard
membership. An empty, duplicate, unknown, uppercase, or vision-account selection
fails startup rather than silently falling back. If the variable is omitted,
registered accounts except the retired `*_vision` cohort are selected. Account
IDs still include archived records, so removing a dashboard card never changes
existing IDs or deletes ledger history.

The decision timer skips unselected, manually paused, non-active, unconfigured,
and already-processed accounts. The `/webhook` route also rejects new decisions
for unselected accounts, including callers that supply an explicit decision.
Decision feeds cache numeric-only cohort bars. The dashboard and JSON payload
contain numeric accounts and numeric trades, without split-test metrics.

Execution candles deliberately still reach registered retired accounts. This
keeps any already-open position's stop/target management intact while new
entries are blocked. Retire old accounts with the ledger's `set_pause` operation;
do not delete accounts or manufacture flatten prices to tidy up the dashboard.

## n8n

Keep the existing five numeric ProDex workflows published. They consume numeric
snapshots and contain no chart-fetch, image-input, or image-archive branch.
Keep the five former image-plus-numeric workflows unpublished/archived, remove
their private `N8N_OVERSEER_URL_*_VISION` routes, and do not restore chart-archive
branches to numeric workflows. Historical exports and execution results are
retained privately. Unrelated n8n workflows/datafeeds are not part of this change.

The former paired workflow deployment, route configurator, comparison checker,
vision patch installer, and chart-archive restoration tools have been removed
from the active source tree. Git history preserves them for forensic reference,
not as a supported operating path.

## Safe deployment and verification

1. Back up the private simulator configuration, ledger using SQLite's backup API,
   and n8n workflow definitions. Keep secret-bearing backups private and verify
   digests before changing runtime state.
2. Confirm `BROKER_MODE=sim`, the exact active ledger path, and no in-flight
   decision service. Preserve numeric balances, generations, pending orders,
   positions, historical decisions, trades, and result-outbox rows.
3. Remove only retired routes, set the numeric allowlist, and pause retired
   accounts through the ledger API. Keep one-minute feeds and the results
   publisher running.
4. Deploy the reviewed bridge commit and restart only the simulation bridge.
   Numeric workflows do not need rewriting or a global n8n restart.
5. Verify `/healthz` reports `broker_mode=sim`; authenticated `/sim/dashboard/data`
   lists the five numeric accounts and no `pairs`; HTML has the numeric-only
   banner and no comparison panel; new vision decisions return 422; the next
   decision timer run uses only numeric routes.
6. Run `python -m pytest tests -q` in an isolated environment with
   `requirements-sim.txt` plus `pytest`. Regression coverage includes archived
   trade filtering before LIMIT and continued retired-position bracket execution.

Do not resume image experiments as an automatic rollback. Preserve backups and
request an explicit operator reversal before restoring that scope.
