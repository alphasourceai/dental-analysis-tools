-- Reconcile Upload.paid for uploads linked to already-paid admin checkout sessions.
-- This is limited to local checkout-session upload links and legacy checkout upload_id values.

with paid_checkout_uploads as (
    select distinct session_uploads.upload_id
    from stripe_checkout_sessions sessions
    join stripe_checkout_session_uploads session_uploads
        on session_uploads.checkout_session_id = sessions.id
    where session_uploads.upload_id is not null
        and (
            lower(coalesce(sessions.payment_status, '')) = 'paid'
            or lower(coalesce(sessions.status, '')) in ('complete', 'completed')
        )

    union

    select distinct sessions.upload_id
    from stripe_checkout_sessions sessions
    where sessions.upload_id is not null
        and (
            lower(coalesce(sessions.payment_status, '')) = 'paid'
            or lower(coalesce(sessions.status, '')) in ('complete', 'completed')
        )
)
update uploads
set paid = true
where id in (select upload_id from paid_checkout_uploads)
    and paid is false;
