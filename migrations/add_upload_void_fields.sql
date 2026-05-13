-- Add soft-void metadata for legacy/report uploads.
-- This preserves upload rows and storage objects while hiding voided uploads from normal workflows.

alter table uploads
    add column if not exists voided_at timestamptz,
    add column if not exists voided_by_admin_user_id text,
    add column if not exists voided_by_admin_email text,
    add column if not exists void_reason text;

create index if not exists uploads_voided_at_idx
    on uploads (voided_at);

create index if not exists uploads_user_email_voided_at_idx
    on uploads (lower(user_email), voided_at);
