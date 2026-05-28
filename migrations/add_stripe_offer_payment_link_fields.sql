-- Additive metadata fields for admin-generated offer payment links.
-- This preserves existing upload checkout behavior and does not add subscription handling.

alter table stripe_checkout_sessions
    add column if not exists description text,
    add column if not exists offer_type varchar(100),
    add column if not exists offer_name text,
    add column if not exists billing_mode varchar(50),
    add column if not exists interval varchar(50),
    add column if not exists internal_note text,
    add column if not exists offer_metadata jsonb;

create index if not exists stripe_checkout_sessions_offer_type_idx
    on stripe_checkout_sessions (offer_type);

create index if not exists stripe_checkout_sessions_billing_mode_idx
    on stripe_checkout_sessions (billing_mode);
