# Paired numeric and chart-image simulator trial

The Pi runs five existing numeric ProDex strategies and five separate chart-image variants. Each pair keeps the same decision cadence, MES bracket choices, 50K Combine rules, and one-minute execution feed. Chart imagery is the intended model-input difference. This is simulated trading only.

| Pair | Decision bar | Numeric account | Image account |
| --- | --- | --- | --- |
| Alpha | 5m | `alpha` | `alpha_vision` |
| Beta | 5m | `beta` | `beta_vision` |
| Gamma | 5m | `gamma` | `gamma_vision` |
| Delta | 15m | `delta` | `delta_vision` |
| Epsilon | 30m | `epsilon` | `epsilon_vision` |

The five new accounts are defined in `tradingview-bot-v2/profiles/paired-vision-1m.json`. They begin at $50,000, use the same three MES bracket choices as their numeric counterparts, and receive the same closed one-minute candles for fills and stops. `SIM_DECISION_SOURCE=scheduler` calls each configured n8n webhook once per fresh strategy candle. The Pi's private bridge environment maps `N8N_OVERSEER_URL_<ACCOUNT>_VISION` to each new webhook. Restart the bridge after adding accounts so its account map refreshes. The original ProjectX service stays disabled.

## Image workflow template

The user's repaired 5-minute alpha workflow is the source for the chart fetch path. The image variants insert this path between `Continuity Context` and the model:

1. Query the cached Chart-Img URL by `symbol` and decision timeframe.
2. If it is younger than 4.8, 14.8, or 29.8 minutes for 5m, 15m, or 30m, fetch it. Otherwise request a new chart with the corresponding `interval`, update the cache, and fetch its JPEG.
3. Preserve binary property `data` through `set url` and send it to the standalone `ProDex Vision` agent with `useInputImage=true` and `imageBinaryProperty=data`.
4. Parse the agent's `output` as the same JSON decision shape used by numeric controls, insert into `ai_trading_log`, and respond to the simulated broker.
5. Archive a JPEG in the `tradingview-chart` Google Cloud Storage bucket and write its permanent URL to `ai_trading_log.urls` after the response. Archive delay cannot delay the broker response.

The ProDex Chat Model used by n8n's Basic LLM Chain drops image pixels even when the chain's message type is `imageBinary`. The standalone ProDex node has a small local patch (`scripts/patch_prodex_vision.py`) that writes the incoming image to a private temporary file, passes it as a Codex `local_image` input, reports `imageIncluded`, and deletes the temporary file. The patch was tested with a direct image recognition check and an isolated n8n webhook before deployment. It applies to ProDex 0.5.1 and may need review after a package upgrade. Its working directory, `/home/node/.n8n/prodex-trading-workspace`, must exist in the n8n container.

Both groups use ProDex's `gpt-6-sol` model with medium reasoning. Numeric controls still use the n8n Basic LLM Chain while image variants use the standalone ProDex node, because the chain drops images. This model-wrapper difference can affect outputs, so the current trial is exploratory rather than an image-only causal test.

Chart-Img session headers, Google OAuth credentials, Supabase credentials, and webhook secrets remain in the private n8n/Pi configuration. Do not export them into this repository. The cache is shared by timeframe, but concurrent workflows can still request duplicate images at a candle boundary; monitor Chart-Img usage.

## Validation and interpretation

At the 2026-09-24 06:10 UTC 5-minute boundary, alpha, beta, and gamma each produced a successful numeric and image decision. Delta did so at 06:15 and 06:30 UTC. Each image agent reported `imageIncluded=true`, responded to the broker, saved an `ai_trading_log` row with a distinct vision `prompt_version`, and logged a permanent chart URL. The broker ledger showed all ten accounts receiving the same one-minute candle. Epsilon's first scheduled run at 06:30 UTC completed and logged its chart, but its model reported `imageIncluded=false` because an open editor overwrote its image settings. After closing the editor and republishing, the saved and active versions both had the image flag and binary property. An isolated n8n run of epsilon's image node reported `imageIncluded=true` using an archived 30-minute chart and made no broker or Supabase writes. Confirm its next scheduled run before counting epsilon as an image result.

The existing numeric accounts have earlier positions and P&L; the new image accounts began flat at $50,000. Raw lifetime P&L is therefore not a fair pair comparison. Compare decisions on identical closed bars immediately, and compare forward net P&L, drawdown, fill count, and decision latency from a recorded common start time. Account context can still differ when one side takes a trade, so a later clean trial should start both sides of each pair from fresh, synchronized ledgers. Do not reset an account with an open position just to force alignment.
