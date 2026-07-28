-- Add first-party public website analytics and contact lead capture.
-- The Admin API exclusively writes these rows. Do not grant browser roles access.

create extension if not exists pgcrypto;

create table if not exists public_analytics_events (
    id uuid primary key default gen_random_uuid(),
    event_name text not null,
    anonymous_id text null,
    session_id text null,
    path text null,
    page_title text null,
    referrer_path text null,
    utm jsonb not null default '{}'::jsonb,
    properties jsonb not null default '{}'::jsonb,
    occurred_at timestamptz not null default now(),
    request_id text null,
    created_at timestamptz not null default now()
);

create index if not exists public_analytics_events_event_occurred_idx
    on public_analytics_events (event_name, occurred_at desc);
create index if not exists public_analytics_events_path_occurred_idx
    on public_analytics_events (path, occurred_at desc);
create index if not exists public_analytics_events_session_occurred_idx
    on public_analytics_events (session_id, occurred_at desc);

alter table public_analytics_events enable row level security;
revoke all on table public_analytics_events from anon, authenticated;

create table if not exists public_lead_drafts (
    id uuid primary key,
    status text not null check (status in ('partial', 'abandoned', 'submitted')),
    form_id text null,
    form_type text null,
    product_interest text null,
    first_name text null,
    last_name text null,
    email text null,
    phone text null,
    message text null,
    fields_completed jsonb not null default '[]'::jsonb,
    last_field text null,
    source_path text null,
    source_referrer_path text null,
    source_cta text null,
    utm jsonb not null default '{}'::jsonb,
    anonymous_id text null,
    session_id text null,
    privacy_notice_version text null,
    request_id text null,
    submitted_at timestamptz null,
    expires_at timestamptz not null default (now() + interval '90 days'),
    archived_at timestamptz null,
    archived_by_user_id text null,
    archive_reason text null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists public_lead_drafts_status_updated_idx
    on public_lead_drafts (status, updated_at desc);
create index if not exists public_lead_drafts_email_idx
    on public_lead_drafts (lower(email))
    where email is not null;
create index if not exists public_lead_drafts_source_updated_idx
    on public_lead_drafts (source_path, updated_at desc);
create index if not exists public_lead_drafts_archived_updated_idx
    on public_lead_drafts (archived_at, updated_at desc);

alter table public_lead_drafts enable row level security;
revoke all on table public_lead_drafts from anon, authenticated;
