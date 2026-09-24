-- Run with psql after restoring the local Supabase database if new
-- ai_trading_log rows receive IDs below restored historical rows.
-- This sequence is shared by the tables below in the current local schema.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '10s';
LOCK TABLE public.ai_trading_log, public.charts, public.governance,
  public.trade_results, public.tweets, public.tweets_financial,
  public.tweets_inspirational IN SHARE ROW EXCLUSIVE MODE;
WITH maxima AS (
  SELECT GREATEST(
    COALESCE((SELECT MAX(ai_decision_id) FROM public.ai_trading_log), 0),
    COALESCE((SELECT MAX(id) FROM public.charts), 0),
    COALESCE((SELECT MAX(id) FROM public.governance), 0),
    COALESCE((SELECT MAX(id) FROM public.trade_results), 0),
    COALESCE((SELECT MAX(id) FROM public.tweets), 0),
    COALESCE((SELECT MAX(id) FROM public.tweets_financial), 0),
    COALESCE((SELECT MAX(id) FROM public.tweets_inspirational), 0),
    (SELECT last_value FROM public.documents_id_seq)
  ) AS highest_id
)
SELECT highest_id, setval('public.documents_id_seq'::regclass, highest_id, true) AS sequence_now
FROM maxima;
COMMIT;
SELECT last_value, is_called FROM public.documents_id_seq;
