-- Add entry/exit pricing to trade_results for richer reporting
ALTER TABLE IF EXISTS trade_results
    ADD COLUMN IF NOT EXISTS entry_price numeric NULL,
    ADD COLUMN IF NOT EXISTS exit_price numeric NULL,
    ADD COLUMN IF NOT EXISTS entry_price_source text NULL,
    ADD COLUMN IF NOT EXISTS exit_price_source text NULL;

-- If ai_trade_feed is a TABLE, add matching columns
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'ai_trade_feed' AND table_schema = 'public'
    ) THEN
        EXECUTE 'ALTER TABLE ai_trade_feed
            ADD COLUMN IF NOT EXISTS entry_price numeric NULL,
            ADD COLUMN IF NOT EXISTS exit_price numeric NULL';
    END IF;
END $$;

-- If ai_trade_feed is a VIEW, recreate it to pass through the new columns
-- Example (adjust column list to your existing definition):
-- CREATE OR REPLACE VIEW ai_trade_feed AS
-- SELECT
--     tr.ai_decision_id,
--     tr.entry_time,
--     tr.exit_time,
--     tr.account,
--     tr.symbol,
--     tr.signal,
--     tr.size,
--     tr.strategy,
--     tr.total_pnl,
--     tr.net_pnl,
--     tr.fees_total,
--     tr.entry_price,
--     tr.exit_price,
--     tr.entry_price_source,
--     tr.exit_price_source,
--     tr.order_id,
--     tr.trace_id,
--     tr.session_id,
--     tr.raw_trades,
--     tr.comment,
--     tr.updated_at
-- FROM trade_results tr;
