begin;

alter table public_analytics_events
    add column if not exists referrer_host text null;

alter table public_lead_drafts
    add column if not exists source_referrer_host text null;

create index if not exists public_analytics_events_referrer_host_occurred_idx
    on public_analytics_events (referrer_host, occurred_at desc)
    where referrer_host is not null;

revoke all on table public_analytics_events from anon, authenticated;
revoke all on table public_lead_drafts from anon, authenticated;

commit;
