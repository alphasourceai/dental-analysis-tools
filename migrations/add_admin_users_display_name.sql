-- Add optional display name metadata for Supabase Auth-backed admin access rows.
-- This does not change existing admin roles, permissions, or runtime behavior.

alter table admin_users
    add column if not exists display_name text;
