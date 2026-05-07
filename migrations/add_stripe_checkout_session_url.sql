-- Add local storage for admin-created Stripe Checkout URLs.
-- This does not change public checkout, report gating, report delivery, or Upload.paid behavior.

alter table stripe_checkout_sessions
    add column if not exists checkout_url text;
