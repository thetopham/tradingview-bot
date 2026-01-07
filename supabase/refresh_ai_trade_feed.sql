begin;

alter table public.ai_trade_feed
  add column if not exists exit_reason text,
  add column if not exists exit_ai_decision_id bigint,
  add column if not exists exit_signal text,
  add column if not exists exit_trigger text;

create or replace function public.refresh_ai_trade_feed()
returns void
language plpgsql
as $$
begin
  truncate table public.ai_trade_feed;

  insert into public.ai_trade_feed (
    ai_decision_id,
    decision_time,
    entry_time,
    exit_time,
    account,
    symbol,
    signal,
    size,
    strategy,
    reason,
    screenshot_url,
    urls,
    total_pnl,
    fees_total,
    net_pnl,
    entry_price,
    exit_price,
    decision_json,
    updated_at,
    exit_reason,
    exit_ai_decision_id,
    exit_signal,
    exit_trigger
  )
  select
    atl.ai_decision_id,
    coalesce(atl.decision_time, atl.created_at) as decision_time,
    tr.entry_time,
    tr.exit_time,
    atl.account,
    atl.symbol,
    atl.signal,
    atl.size,
    atl.strategy,
    atl.reason,
    atl.screenshot_url,
    atl.urls,
    tr.total_pnl,
    tr.fees_total,
    tr.net_pnl,
    tr.entry_price,
    tr.exit_price,
    atl.decision_json,
    coalesce(tr.updated_at, atl.updated_at, atl.created_at) as updated_at,
    tr.exit_reason,
    tr.exit_ai_decision_id,
    tr.exit_signal,
    tr.exit_trigger
  from public.ai_trading_log atl
  left join public.trade_results tr
    on tr.ai_decision_id = atl.ai_decision_id;
end;
$$;

commit;
