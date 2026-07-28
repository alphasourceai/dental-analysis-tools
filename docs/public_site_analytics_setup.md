# Public Site Analytics and Lead Capture Setup

Apply `migrations/add_public_site_analytics_and_leads.sql` to the Consulting database before deploying the API. The migration creates private server-managed tables for consented public-site events and public contact-form lead captures. Browser `anon` and `authenticated` roles have no access to either table.

Configure the Admin API service with:

```text
PUBLIC_SITE_ALLOWED_ORIGINS=https://alphasourceconsulting.com,https://www.alphasourceconsulting.com
```

For local development, add only the exact local frontend origin that is needed. The public API accepts only these origins for browser requests.

Configure the public website build with:

```text
VITE_SITE_API_BASE_URL=https://alphasource-consulting-admin-api.onrender.com
```

`VITE_ADMIN_API_BASE_URL` is supported as a compatibility fallback. Do not set a service-role key or any other secret in a `VITE_` variable. Set `VITE_PUBLIC_ANALYTICS_ENABLED=false` to disable optional public analytics while leaving contact form delivery available.

The existing optional `VITE_CONTACT_FORM_ENDPOINT` remains a non-blocking compatibility delivery path. The first-party API is the system of record for the Admin Site Analytics page.
