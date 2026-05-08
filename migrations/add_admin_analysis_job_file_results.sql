alter table admin_analysis_job_files
    add column if not exists analysis_data text;

alter table admin_analysis_job_files
    add column if not exists processed_at timestamptz;
