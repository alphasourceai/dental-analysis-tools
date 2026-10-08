-- These RLS-protected business tables are accessed only by server-side code.
-- Remove default Data API grants so future policies cannot expose them by accident.

revoke all on table
    public.admin_analysis_job_files,
    public.admin_analysis_jobs,
    public.admin_analysis_phi_acknowledgments,
    public.admin_audit_events,
    public.billing_overrides,
    public.consulting_agreements,
    public.stripe_checkout_session_uploads,
    public.stripe_checkout_sessions,
    public.stripe_customers,
    public.stripe_events,
    public.stripe_payments,
    public.stripe_subscriptions
from anon, authenticated;
