-- Add admin Document Analysis PHI/HIPAA processing acknowledgment audit records.
-- This does not alter processing output, Secure Uploads, public analyzer, billing, delivery, or reporting behavior.

create table if not exists admin_analysis_phi_acknowledgments (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references admin_analysis_jobs(id) on delete cascade,
    job_file_id uuid references admin_analysis_job_files(id) on delete set null,
    tool_name text,
    admin_user_id text,
    admin_email text,
    initials text not null,
    confirmed_no_phi boolean not null,
    acknowledgment_text text not null,
    acknowledgment_version varchar(100) not null,
    ip_address text,
    user_agent text,
    created_at timestamptz not null default now()
);

create index if not exists admin_analysis_phi_acknowledgments_job_id_idx
    on admin_analysis_phi_acknowledgments (job_id);
create index if not exists admin_analysis_phi_acknowledgments_job_file_id_idx
    on admin_analysis_phi_acknowledgments (job_file_id);
create index if not exists admin_analysis_phi_acknowledgments_admin_user_id_idx
    on admin_analysis_phi_acknowledgments (admin_user_id);
create index if not exists admin_analysis_phi_acknowledgments_created_at_idx
    on admin_analysis_phi_acknowledgments (created_at);
