-- Upload portal tables for magic-link intake (GCS signed uploads)

create table if not exists upload_portal_requests (
    id uuid primary key default gen_random_uuid(),
    requester_email text not null,
    token_hash text not null unique,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    used_at timestamptz,
    request_ip text
);

create index if not exists upload_portal_requests_email_idx
    on upload_portal_requests (lower(requester_email));
create index if not exists upload_portal_requests_expires_idx
    on upload_portal_requests (expires_at);

create table if not exists upload_portal_sessions (
    id uuid primary key default gen_random_uuid(),
    request_id uuid not null references upload_portal_requests(id) on delete cascade,
    token_hash text not null unique,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    last_used_at timestamptz
);

create index if not exists upload_portal_sessions_request_idx
    on upload_portal_sessions (request_id);
create index if not exists upload_portal_sessions_expires_idx
    on upload_portal_sessions (expires_at);

create table if not exists upload_portal_files (
    id uuid primary key default gen_random_uuid(),
    request_id uuid not null references upload_portal_requests(id) on delete cascade,
    session_id uuid not null references upload_portal_sessions(id) on delete cascade,
    user_id uuid references users(id),
    user_email text,
    gcs_bucket text not null,
    object_name text not null,
    original_filename text not null,
    content_type text,
    byte_size bigint,
    created_at timestamptz not null default now(),
    completed_at timestamptz
);

create index if not exists upload_portal_files_request_idx
    on upload_portal_files (request_id);
create index if not exists upload_portal_files_session_idx
    on upload_portal_files (session_id);
create index if not exists upload_portal_files_user_idx
    on upload_portal_files (user_id);
create index if not exists upload_portal_files_created_idx
    on upload_portal_files (created_at);
