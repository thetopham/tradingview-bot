-- Add entry/exit price capture to trade_results and ai_trade_feed.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'trade_results') THEN
        ALTER TABLE trade_results ADD COLUMN IF NOT EXISTS entry_price numeric NULL;
        ALTER TABLE trade_results ADD COLUMN IF NOT EXISTS exit_price numeric NULL;
        ALTER TABLE trade_results ADD COLUMN IF NOT EXISTS entry_price_source text NULL;
        ALTER TABLE trade_results ADD COLUMN IF NOT EXISTS exit_price_source text NULL;
    END IF;

    -- If ai_trade_feed is a table, mirror the new columns for dashboard use.
    IF EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
          AND n.nspname = 'public'
          AND c.relname = 'ai_trade_feed'
    ) THEN
        ALTER TABLE public.ai_trade_feed ADD COLUMN IF NOT EXISTS entry_price numeric NULL;
        ALTER TABLE public.ai_trade_feed ADD COLUMN IF NOT EXISTS exit_price numeric NULL;
    END IF;
END $$;
