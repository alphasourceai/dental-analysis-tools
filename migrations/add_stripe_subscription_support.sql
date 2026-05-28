-- Additive subscription support for admin-generated recurring retainer checkout links.
-- Existing upload checkout and one-time offer payment links remain unchanged.

alter table stripe_checkout_sessions
    add column if not exists stripe_subscription_id text,
    add column if not exists contract_months integer,
    add column if not exists monthly_amount integer,
    add column if not exists subscription_status varchar(50),
    add column if not exists current_period_end timestamptz,
    add column if not exists cancel_at timestamptz;

create index if not exists stripe_checkout_sessions_subscription_id_idx
    on stripe_checkout_sessions (stripe_subscription_id);

create index if not exists stripe_checkout_sessions_subscription_status_idx
    on stripe_checkout_sessions (subscription_status);

create table if not exists stripe_subscriptions (
    id uuid primary key default gen_random_uuid(),
    client_email text not null,
    user_id uuid references users(id),
    stripe_customer_id text,
    stripe_subscription_id text unique,
    source_checkout_session_id uuid references stripe_checkout_sessions(id),
    stripe_checkout_session_id text,
    offer_type text,
    offer_name text,
    billing_mode text default 'recurring',
    interval text default 'month',
    monthly_amount integer,
    currency text default 'usd',
    contract_months integer,
    status text,
    current_period_start timestamptz,
    current_period_end timestamptz,
    cancel_at timestamptz,
    cancel_at_period_end boolean,
    canceled_at timestamptz,
    latest_invoice_id text,
    latest_payment_status text,
    internal_note text,
    metadata jsonb,
    livemode boolean not null default false,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists stripe_subscriptions_client_email_idx
    on stripe_subscriptions (client_email);

create index if not exists stripe_subscriptions_user_id_idx
    on stripe_subscriptions (user_id);

create index if not exists stripe_subscriptions_customer_idx
    on stripe_subscriptions (stripe_customer_id);

create index if not exists stripe_subscriptions_stripe_subscription_idx
    on stripe_subscriptions (stripe_subscription_id);

create index if not exists stripe_subscriptions_checkout_session_idx
    on stripe_subscriptions (source_checkout_session_id);

create index if not exists stripe_subscriptions_status_idx
    on stripe_subscriptions (status);

create index if not exists stripe_subscriptions_offer_type_idx
    on stripe_subscriptions (offer_type);
