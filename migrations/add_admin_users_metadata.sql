-- Add optional metadata for Supabase Auth-backed admin access rows.
-- This does not change existing admin roles, permissions, or runtime behavior.

alter table admin_users
    add column if not exists email text;

alter table admin_users
    add column if not exists status text not null default 'active';

alter table admin_users
    add column if not exists created_at timestamptz not null default now();

alter table admin_users
    add column if not exists updated_at timestamptz not null default now();

alter table admin_users
    add column if not exists created_by uuid null;

alter table admin_users
    add column if not exists deactivated_at timestamptz null;

create index if not exists admin_users_email_lower_idx
    on admin_users (lower(email))
    where email is not null;

create index if not exists admin_users_status_idx
    on admin_users (status);

create index if not exists admin_users_role_idx
    on admin_users (role);
