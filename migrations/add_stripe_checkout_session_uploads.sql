-- Add multi-upload associations for admin-created Stripe Checkout Sessions.
-- This preserves the legacy stripe_checkout_sessions.upload_id field for backward compatibility.

create table if not exists stripe_checkout_session_uploads (
    id uuid primary key default gen_random_uuid(),
    checkout_session_id uuid not null references stripe_checkout_sessions(id) on delete cascade,
    upload_id uuid not null references uploads(id),
    created_at timestamptz not null default now()
);

create unique index if not exists stripe_checkout_session_uploads_session_upload_idx
    on stripe_checkout_session_uploads (checkout_session_id, upload_id);

create index if not exists stripe_checkout_session_uploads_session_idx
    on stripe_checkout_session_uploads (checkout_session_id);

create index if not exists stripe_checkout_session_uploads_upload_idx
    on stripe_checkout_session_uploads (upload_id);

insert into stripe_checkout_session_uploads (checkout_session_id, upload_id)
select id, upload_id
from stripe_checkout_sessions
where upload_id is not null
on conflict (checkout_session_id, upload_id) do nothing;
