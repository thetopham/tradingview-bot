-- Adds entry/exit price tracking to closed trades and feed
ALTER TABLE IF EXISTS trade_results ADD COLUMN IF NOT EXISTS entry_price numeric NULL;
ALTER TABLE IF EXISTS trade_results ADD COLUMN IF NOT EXISTS exit_price numeric NULL;
ALTER TABLE IF EXISTS trade_results ADD COLUMN IF NOT EXISTS entry_price_source text NULL;
ALTER TABLE IF EXISTS trade_results ADD COLUMN IF NOT EXISTS exit_price_source text NULL;

-- If ai_trade_feed is a table, add passthrough columns
ALTER TABLE IF EXISTS ai_trade_feed ADD COLUMN IF NOT EXISTS entry_price numeric NULL;
ALTER TABLE IF EXISTS ai_trade_feed ADD COLUMN IF NOT EXISTS exit_price numeric NULL;

-- If ai_trade_feed is a view, update it to include the new columns from trade_results.
-- Example (adapt to your current view definition):
-- CREATE OR REPLACE VIEW ai_trade_feed AS
-- SELECT atl.*, tr.entry_time, tr.exit_time, tr.total_pnl, tr.net_pnl, tr.fees_total,
--        tr.entry_price, tr.exit_price
-- FROM ai_trading_log atl
-- LEFT JOIN trade_results tr ON tr.ai_decision_id = atl.ai_decision_id;
