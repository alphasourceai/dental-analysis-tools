-- Add GHL tracking fields for public analyzer lead handoff/writeback.
-- Columns are nullable so existing submissions and non-GHL flows remain unchanged.

alter table client_submissions
    add column if not exists ghl_cid text,
    add column if not exists ghl_analyzer_submitted_at timestamptz,
    add column if not exists ghl_analyzer_submitted_error text;

create index if not exists client_submissions_ghl_cid_idx
    on client_submissions (ghl_cid)
    where ghl_cid is not null;
