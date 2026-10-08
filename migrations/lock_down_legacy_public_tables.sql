-- Legacy application tables are accessed through the server-side Admin API.
-- Block direct Data API access by browser roles without changing server-side SQL.

alter table public.users enable row level security;
alter table public.admins enable row level security;
alter table public.uploads enable row level security;
alter table public.admin_users enable row level security;
alter table public.client_submissions enable row level security;
alter table public.upload_files enable row level security;
alter table public.upload_portal_requests enable row level security;
alter table public.upload_portal_sessions enable row level security;
alter table public.upload_portal_files enable row level security;

revoke all on table
    public.users,
    public.admins,
    public.uploads,
    public.admin_users,
    public.client_submissions,
    public.upload_files,
    public.upload_portal_requests,
    public.upload_portal_sessions,
    public.upload_portal_files
from anon, authenticated;
