-- Add Stripe-sourced expiration metadata for admin-created Checkout Sessions.
-- Expiration state remains Stripe-sourced through webhook/manual expire API responses.

alter table stripe_checkout_sessions
    add column if not exists expires_at timestamptz,
    add column if not exists expired_at timestamptz;

create index if not exists stripe_checkout_sessions_expires_at_idx
    on stripe_checkout_sessions (expires_at);

create index if not exists stripe_checkout_sessions_expired_at_idx
    on stripe_checkout_sessions (expired_at);
