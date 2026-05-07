-- Additive Stripe billing foundation.
-- This does not change public analyzer access, report delivery, or Upload.paid behavior.

alter table users
    add column if not exists stripe_customer_id text;

create unique index if not exists users_stripe_customer_id_idx
    on users (stripe_customer_id)
    where stripe_customer_id is not null;

create table if not exists stripe_events (
    id uuid primary key default gen_random_uuid(),
    stripe_event_id text not null unique,
    event_type text not null,
    livemode boolean not null default false,
    api_version text,
    processing_status varchar(50) not null default 'received',
    error_message text,
    received_at timestamptz not null default now(),
    processed_at timestamptz,
    payload text
);

create index if not exists stripe_events_event_type_idx
    on stripe_events (event_type);
create index if not exists stripe_events_processing_status_idx
    on stripe_events (processing_status);
create index if not exists stripe_events_received_at_idx
    on stripe_events (received_at);

create table if not exists stripe_customers (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references users(id),
    client_email text,
    stripe_customer_id text unique,
    livemode boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists stripe_customers_user_id_idx
    on stripe_customers (user_id);
create index if not exists stripe_customers_client_email_idx
    on stripe_customers (client_email);

create table if not exists stripe_checkout_sessions (
    id uuid primary key default gen_random_uuid(),
    stripe_checkout_session_id text unique,
    stripe_customer_id text,
    client_email text,
    user_id uuid references users(id),
    client_submission_id uuid references client_submissions(id),
    upload_id uuid references uploads(id),
    purpose varchar(100),
    mode varchar(50),
    status varchar(50),
    payment_status varchar(50),
    amount_total integer,
    currency varchar(10),
    success_url text,
    cancel_url text,
    livemode boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists stripe_checkout_sessions_customer_idx
    on stripe_checkout_sessions (stripe_customer_id);
create index if not exists stripe_checkout_sessions_client_email_idx
    on stripe_checkout_sessions (client_email);
create index if not exists stripe_checkout_sessions_user_id_idx
    on stripe_checkout_sessions (user_id);
create index if not exists stripe_checkout_sessions_submission_id_idx
    on stripe_checkout_sessions (client_submission_id);
create index if not exists stripe_checkout_sessions_upload_id_idx
    on stripe_checkout_sessions (upload_id);
create index if not exists stripe_checkout_sessions_status_idx
    on stripe_checkout_sessions (status);
create index if not exists stripe_checkout_sessions_payment_status_idx
    on stripe_checkout_sessions (payment_status);

create table if not exists stripe_payments (
    id uuid primary key default gen_random_uuid(),
    stripe_payment_intent_id text unique,
    stripe_checkout_session_id text,
    stripe_invoice_id text,
    client_email text,
    upload_id uuid references uploads(id),
    status varchar(50),
    amount integer,
    amount_received integer,
    amount_refunded integer,
    currency varchar(10),
    paid_at timestamptz,
    failed_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists stripe_payments_checkout_session_idx
    on stripe_payments (stripe_checkout_session_id);
create index if not exists stripe_payments_invoice_idx
    on stripe_payments (stripe_invoice_id);
create index if not exists stripe_payments_client_email_idx
    on stripe_payments (client_email);
create index if not exists stripe_payments_upload_id_idx
    on stripe_payments (upload_id);
create index if not exists stripe_payments_status_idx
    on stripe_payments (status);

create table if not exists billing_overrides (
    id uuid primary key default gen_random_uuid(),
    target_type varchar(100),
    target_id text,
    client_email text,
    override_paid boolean,
    reason text,
    admin_user_id text,
    created_at timestamptz not null default now()
);

create index if not exists billing_overrides_target_idx
    on billing_overrides (target_type, target_id);
create index if not exists billing_overrides_client_email_idx
    on billing_overrides (client_email);
create index if not exists billing_overrides_admin_user_id_idx
    on billing_overrides (admin_user_id);
