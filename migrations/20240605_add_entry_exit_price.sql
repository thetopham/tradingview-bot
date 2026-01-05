-- Add entry/exit prices and sources to trade_results
ALTER TABLE IF EXISTS trade_results
    ADD COLUMN IF NOT EXISTS entry_price numeric NULL,
    ADD COLUMN IF NOT EXISTS exit_price numeric NULL,
    ADD COLUMN IF NOT EXISTS entry_price_source text NULL,
    ADD COLUMN IF NOT EXISTS exit_price_source text NULL;

-- Add entry/exit prices to ai_trade_feed table (if materialized as a table)
ALTER TABLE IF EXISTS ai_trade_feed
    ADD COLUMN IF NOT EXISTS entry_price numeric NULL,
    ADD COLUMN IF NOT EXISTS exit_price numeric NULL;

-- If ai_trade_feed is a view, recreate it to include entry_price/exit_price from trade_results
-- Example (adjust column list to your environment):
-- CREATE OR REPLACE VIEW ai_trade_feed AS
-- SELECT l.ai_decision_id,
--        l.timestamp AS decision_time,
--        r.entry_time,
--        r.exit_time,
--        r.entry_price,
--        r.exit_price,
--        r.account,
--        r.symbol,
--        r.signal,
--        r.size,
--        r.strategy,
--        l.reason,
--        l.screenshot_url,
--        l.urls,
--        r.total_pnl,
--        r.fees_total,
--        r.net_pnl,
--        l.decision_json,
--        r.updated_at
-- FROM ai_trading_log l
-- LEFT JOIN trade_results r ON r.ai_decision_id = l.ai_decision_id;
