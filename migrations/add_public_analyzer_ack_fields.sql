-- Add public analyzer phone and financial-only acknowledgement metadata.
-- All columns are nullable so existing Streamlit/admin flows continue to work.

alter table users
    add column if not exists phone varchar(50);

alter table client_submissions
    add column if not exists phone varchar(50),
    add column if not exists financial_only_acknowledgement boolean,
    add column if not exists acknowledgement_timestamp timestamptz,
    add column if not exists acknowledgement_ip text,
    add column if not exists acknowledgement_version varchar(100);
