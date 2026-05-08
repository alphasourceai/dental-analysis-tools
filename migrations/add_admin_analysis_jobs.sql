-- Add durable admin Document Analysis job foundation.
-- This does not process uploads, run analysis, send email, call GHL, or modify existing workflow tables.

create table if not exists admin_analysis_jobs (
    id uuid primary key default gen_random_uuid(),
    status varchar(50) not null default 'queued',
    created_by_admin_user_id text,
    client_email text,
    first_name text,
    last_name text,
    office_name text,
    org_type varchar(50),
    phone text,
    ghl_cid text,
    client_mode varchar(50),
    analysis_run_id text unique,
    submission_id uuid references client_submissions(id),
    progress_percent integer not null default 0,
    current_step text,
    error_code text,
    error_message text,
    created_at timestamptz not null default now(),
    started_at timestamptz,
    completed_at timestamptz,
    canceled_at timestamptz,
    errored_at timestamptz,
    updated_at timestamptz not null default now()
);

create index if not exists admin_analysis_jobs_status_idx
    on admin_analysis_jobs (status);
create index if not exists admin_analysis_jobs_client_email_idx
    on admin_analysis_jobs (client_email);
create index if not exists admin_analysis_jobs_created_at_idx
    on admin_analysis_jobs (created_at);
create index if not exists admin_analysis_jobs_analysis_run_id_idx
    on admin_analysis_jobs (analysis_run_id);

create table if not exists admin_analysis_job_files (
    id uuid primary key default gen_random_uuid(),
    job_id uuid not null references admin_analysis_jobs(id) on delete cascade,
    tool_name text not null,
    original_filename text,
    content_type text,
    byte_size bigint,
    upload_file_id uuid references upload_files(id),
    upload_id uuid references uploads(id),
    status varchar(50) not null default 'queued',
    error_code text,
    error_message text,
    created_at timestamptz not null default now(),
    started_at timestamptz,
    completed_at timestamptz,
    errored_at timestamptz
);

create index if not exists admin_analysis_job_files_job_id_idx
    on admin_analysis_job_files (job_id);
create index if not exists admin_analysis_job_files_status_idx
    on admin_analysis_job_files (status);
