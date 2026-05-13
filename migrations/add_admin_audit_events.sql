-- Add unified admin audit events for super-admin audit trail reporting.

create table if not exists admin_audit_events (
    id uuid primary key default gen_random_uuid(),
    occurred_at timestamptz not null default now(),
    source text not null,
    event_type text not null,
    actor_admin_user_id text,
    actor_admin_email text,
    actor_display_name text,
    actor_role text,
    client_email text,
    target_type text,
    target_id text,
    ip_address text,
    user_agent text,
    device_summary text,
    location text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create index if not exists admin_audit_events_occurred_at_idx
    on admin_audit_events (occurred_at desc);

create index if not exists admin_audit_events_event_type_idx
    on admin_audit_events (event_type);

create index if not exists admin_audit_events_actor_user_idx
    on admin_audit_events (actor_admin_user_id);

create index if not exists admin_audit_events_actor_email_lower_idx
    on admin_audit_events (lower(actor_admin_email));

create index if not exists admin_audit_events_client_email_lower_idx
    on admin_audit_events (lower(client_email));

create index if not exists admin_audit_events_target_idx
    on admin_audit_events (target_type, target_id);
