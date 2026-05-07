from __future__ import annotations

import logging
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import stripe
from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, or_

from database import SessionLocal
from models import BillingOverride, ClientSubmission, StripeCheckoutSession, StripeCustomer, StripeEvent, Upload, User
from supabase_utils import get_current_admin_user, is_admin_user

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="Admin API")

allowed_origins = [
    origin.strip()
    for origin in os.getenv("ADMIN_API_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.get("/")
def root() -> dict[str, object]:
    return {"ok": True, "service": "admin-api"}


@app.head("/")
def root_head() -> Response:
    return Response(status_code=200)


@app.get("/health")
def health() -> dict[str, object]:
    return {"ok": True, "service": "admin-api"}


@app.get("/api/admin/me")
def get_admin_me(request: Request) -> JSONResponse:
    user, error_response = _require_admin_user(request)
    if error_response:
        return error_response

    return JSONResponse(
        {
            "ok": True,
            "user": {
                "id": str(user.get("id") or ""),
                "email": str(user.get("email") or ""),
            },
            "role": "admin",
        }
    )


@app.get("/api/admin/clients")
def list_admin_clients(
    request: Request,
    search: Optional[str] = None,
    limit: int = Query(25, ge=1),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    _, error_response = _require_admin_user(request)
    if error_response:
        return error_response

    safe_limit = min(limit, 100)
    normalized_search = (search or "").strip().lower()
    db = SessionLocal()
    try:
        matching_emails: Optional[set[str]] = None
        if normalized_search:
            search_like = f"%{normalized_search}%"
            matching_submission_rows = (
                db.query(ClientSubmission.user_email)
                .filter(
                    or_(
                        ClientSubmission.user_email.ilike(search_like),
                        ClientSubmission.first_name.ilike(search_like),
                        ClientSubmission.last_name.ilike(search_like),
                        ClientSubmission.office_name.ilike(search_like),
                        ClientSubmission.phone.ilike(search_like),
                    )
                )
                .distinct()
                .all()
            )
            matching_user_rows = (
                db.query(User.email)
                .filter(
                    or_(
                        User.email.ilike(search_like),
                        User.first_name.ilike(search_like),
                        User.last_name.ilike(search_like),
                        User.office_name.ilike(search_like),
                        User.phone.ilike(search_like),
                    )
                )
                .distinct()
                .all()
            )
            matching_emails = {
                str(row[0]).strip()
                for row in [*matching_submission_rows, *matching_user_rows]
                if row[0]
            }
            if not matching_emails:
                return _clients_response([], safe_limit, offset, has_more=False)

        clients_query = db.query(
            ClientSubmission.user_email.label("email"),
            func.count(ClientSubmission.id).label("submission_count"),
            func.max(ClientSubmission.submitted_at).label("last_submitted_at"),
        )
        if matching_emails is not None:
            clients_query = clients_query.filter(ClientSubmission.user_email.in_(matching_emails))
        client_rows = (
            clients_query.group_by(ClientSubmission.user_email)
            .order_by(func.max(ClientSubmission.submitted_at).desc())
            .offset(offset)
            .limit(safe_limit + 1)
            .all()
        )
        has_more = len(client_rows) > safe_limit
        client_rows = client_rows[:safe_limit]
        client_emails = [row.email for row in client_rows if row.email]

        upload_counts: dict[str, int] = {}
        latest_submissions: dict[str, ClientSubmission] = {}
        users_by_email: dict[str, User] = {}
        billing_summaries: dict[str, dict[str, Any]] = {}
        if client_emails:
            normalized_client_emails = [
                email.strip().lower()
                for email in client_emails
                if email and email.strip()
            ]
            upload_count_rows = (
                db.query(
                    ClientSubmission.user_email,
                    func.count(Upload.id).label("upload_count"),
                )
                .outerjoin(Upload, Upload.submission_id == ClientSubmission.id)
                .filter(ClientSubmission.user_email.in_(client_emails))
                .group_by(ClientSubmission.user_email)
                .all()
            )
            upload_counts = {row[0]: int(row[1] or 0) for row in upload_count_rows if row[0]}

            billing_summaries = {
                email: _empty_billing_summary()
                for email in normalized_client_emails
            }
            checkout_session_rows = (
                db.query(StripeCheckoutSession)
                .filter(func.lower(StripeCheckoutSession.client_email).in_(normalized_client_emails))
                .order_by(
                    func.lower(StripeCheckoutSession.client_email).asc(),
                    StripeCheckoutSession.created_at.desc(),
                )
                .all()
            )
            for session in checkout_session_rows:
                billing_email = (_clean_text(session.client_email) or "").lower()
                summary = billing_summaries.setdefault(billing_email, _empty_billing_summary())
                payment_status = (_clean_text(session.payment_status) or "").lower()
                status = (_clean_text(session.status) or "").lower()
                summary["checkoutSessionCount"] += 1
                if payment_status == "paid":
                    summary["paidCheckoutSessionCount"] += 1
                if payment_status != "paid" or status == "open":
                    summary["openCheckoutSessionCount"] += 1
                if summary["latestPaymentStatus"] is None:
                    summary["latestPaymentStatus"] = _clean_text(session.payment_status)

            override_count_rows = (
                db.query(
                    func.lower(BillingOverride.client_email).label("client_email"),
                    func.count(BillingOverride.id).label("override_count"),
                )
                .filter(func.lower(BillingOverride.client_email).in_(normalized_client_emails))
                .group_by(func.lower(BillingOverride.client_email))
                .all()
            )
            for row in override_count_rows:
                billing_email = row.client_email
                summary = billing_summaries.setdefault(billing_email, _empty_billing_summary())
                summary["manualOverrideCount"] = int(row.override_count or 0)

            latest_rows = (
                db.query(ClientSubmission)
                .filter(ClientSubmission.user_email.in_(client_emails))
                .order_by(
                    ClientSubmission.user_email.asc(),
                    ClientSubmission.submitted_at.desc(),
                )
                .all()
            )
            for submission in latest_rows:
                if submission.user_email not in latest_submissions:
                    latest_submissions[submission.user_email] = submission

            users = db.query(User).filter(User.email.in_(client_emails)).all()
            users_by_email = {user.email: user for user in users if user.email}

        items = []
        for row in client_rows:
            email = row.email or ""
            latest_submission = latest_submissions.get(email)
            user_record = users_by_email.get(email)
            latest_phone = (
                _clean_text(getattr(latest_submission, "phone", None))
                or _clean_text(getattr(user_record, "phone", None))
                or None
            )
            items.append(
                {
                    "email": email,
                    "latestName": _full_name(latest_submission),
                    "latestOfficeName": _clean_text(getattr(latest_submission, "office_name", None)),
                    "latestOrgType": _clean_text(getattr(latest_submission, "org_type", None)),
                    "latestPhone": latest_phone,
                    "submissionCount": int(row.submission_count or 0),
                    "uploadCount": upload_counts.get(email, 0),
                    "latestSubmittedAt": _iso_datetime(row.last_submitted_at),
                    "latestStatus": _clean_text(getattr(latest_submission, "status", None)),
                    "billing": billing_summaries.get(email.lower(), _empty_billing_summary()),
                }
            )

        return _clients_response(items, safe_limit, offset, has_more=has_more)
    except Exception:
        logger.exception("[admin_api] client list query failed.")
        return _error_response(500, "internal_error", "Unable to load clients.")
    finally:
        db.close()


@app.post("/api/admin/billing/checkout-sessions")
async def create_admin_checkout_session(request: Request) -> JSONResponse:
    admin_user, error_response = _require_admin_user(request)
    if error_response:
        return error_response

    stripe_secret_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not stripe_secret_key:
        logger.error("[admin_api] Stripe secret key is not configured.")
        return _error_response(503, "stripe_not_configured", "Stripe is not configured.")

    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    client_email, validation_error = _required_email(body.get("clientEmail"))
    if validation_error:
        return validation_error
    purpose, validation_error = _required_text(body.get("purpose"), "purpose")
    if validation_error:
        return validation_error
    description, validation_error = _required_text(body.get("description"), "description")
    if validation_error:
        return validation_error
    amount, validation_error = _required_amount(body.get("amount"))
    if validation_error:
        return validation_error
    currency = _clean_text(body.get("currency")) or "usd"
    currency = currency.lower()
    if len(currency) != 3 or not currency.isalpha():
        return _error_response(400, "invalid_currency", "Currency must be a three-letter code.")
    success_url, validation_error = _required_text(body.get("successUrl"), "successUrl")
    if validation_error:
        return validation_error
    cancel_url, validation_error = _required_text(body.get("cancelUrl"), "cancelUrl")
    if validation_error:
        return validation_error
    if not _is_safe_checkout_url(success_url) or not _is_safe_checkout_url(cancel_url):
        return _error_response(400, "invalid_url", "Checkout URLs must use http or https.")
    upload_id, validation_error = _optional_uuid(body.get("uploadId"), "uploadId")
    if validation_error:
        return validation_error
    client_submission_id, validation_error = _optional_uuid(
        body.get("clientSubmissionId"),
        "clientSubmissionId",
    )
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        user_record = db.query(User).filter(func.lower(User.email) == client_email).first()
        stripe_customer_id, livemode = _get_or_create_stripe_customer(
            db=db,
            client_email=client_email,
            user_record=user_record,
            stripe_secret_key=stripe_secret_key,
        )
        metadata = {
            "client_email": client_email,
            "purpose": purpose,
            "created_by_admin_user_id": str(admin_user.get("id") or ""),
            "source": "consulting_admin_api",
        }
        if upload_id:
            metadata["upload_id"] = str(upload_id)
        if client_submission_id:
            metadata["client_submission_id"] = str(client_submission_id)

        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            customer=stripe_customer_id,
            line_items=[
                {
                    "price_data": {
                        "currency": currency,
                        "unit_amount": amount,
                        "product_data": {
                            "name": description,
                        },
                    },
                    "quantity": 1,
                }
            ],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            api_key=stripe_secret_key,
        )
        session_data = _stripe_object_to_dict(checkout_session)
        checkout_session_id = _clean_text(session_data.get("id"))
        checkout_url = _clean_text(session_data.get("url"))
        if not checkout_session_id or not checkout_url:
            logger.error("[admin_api] Stripe checkout session missing id or url.")
            db.rollback()
            return _error_response(502, "stripe_checkout_failed", "Unable to create checkout session.")

        local_session = StripeCheckoutSession(
            stripe_checkout_session_id=checkout_session_id,
            stripe_customer_id=stripe_customer_id,
            client_email=client_email,
            user_id=getattr(user_record, "id", None),
            client_submission_id=client_submission_id,
            upload_id=upload_id,
            purpose=purpose,
            mode=_clean_text(session_data.get("mode")) or "payment",
            status=_clean_text(session_data.get("status")),
            payment_status=_clean_text(session_data.get("payment_status")),
            amount_total=_optional_int(session_data.get("amount_total")) or amount,
            currency=_clean_text(session_data.get("currency")) or currency,
            checkout_url=checkout_url,
            success_url=success_url,
            cancel_url=cancel_url,
            livemode=bool(session_data.get("livemode", livemode)),
        )
        db.add(local_session)
        db.commit()
        logger.info(
            "[admin_api] Stripe checkout session created id=%s client_email=%s purpose=%s amount=%s currency=%s admin_user_id=%s",
            checkout_session_id,
            client_email,
            purpose,
            amount,
            currency,
            str(admin_user.get("id") or ""),
        )
        return JSONResponse(
            {
                "ok": True,
                "checkoutSessionId": checkout_session_id,
                "url": checkout_url,
                "status": _clean_text(session_data.get("status")) or "open",
                "paymentStatus": _clean_text(session_data.get("payment_status")) or "unpaid",
            }
        )
    except stripe.error.StripeError:
        db.rollback()
        logger.exception(
            "[admin_api] Stripe checkout creation failed client_email=%s purpose=%s amount=%s currency=%s",
            client_email,
            purpose,
            amount,
            currency,
        )
        return _error_response(502, "stripe_checkout_failed", "Unable to create checkout session.")
    except Exception:
        db.rollback()
        logger.exception(
            "[admin_api] admin checkout session failed client_email=%s purpose=%s",
            client_email,
            purpose,
        )
        return _error_response(500, "checkout_session_failed", "Unable to create checkout session.")
    finally:
        db.close()


@app.get("/api/admin/billing/client")
def get_admin_billing_client(
    request: Request,
    email: Optional[str] = None,
) -> JSONResponse:
    _, error_response = _require_admin_user(request)
    if error_response:
        return error_response

    client_email, validation_error = _required_email(email)
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        customers = (
            db.query(StripeCustomer)
            .filter(func.lower(StripeCustomer.client_email) == client_email)
            .order_by(StripeCustomer.updated_at.desc())
            .all()
        )
        checkout_sessions = (
            db.query(StripeCheckoutSession)
            .filter(func.lower(StripeCheckoutSession.client_email) == client_email)
            .order_by(StripeCheckoutSession.created_at.desc())
            .all()
        )
        uploads = (
            db.query(Upload)
            .filter(func.lower(Upload.user_email) == client_email)
            .order_by(Upload.id.desc())
            .limit(25)
            .all()
        )
        billing_overrides = (
            db.query(BillingOverride)
            .filter(func.lower(BillingOverride.client_email) == client_email)
            .order_by(BillingOverride.created_at.desc())
            .limit(25)
            .all()
        )

        paid_sessions = [
            session
            for session in checkout_sessions
            if (_clean_text(session.payment_status) or "").lower() == "paid"
        ]
        open_sessions = [
            session
            for session in checkout_sessions
            if (_clean_text(session.payment_status) or "").lower() != "paid"
            or (_clean_text(session.status) or "").lower() == "open"
        ]
        latest_session = checkout_sessions[0] if checkout_sessions else None
        latest_paid_session = paid_sessions[0] if paid_sessions else None

        return JSONResponse(
            {
                "ok": True,
                "clientEmail": client_email,
                "customer": _stripe_customer_payload(customers[0]) if customers else None,
                "customers": [_stripe_customer_payload(customer) for customer in customers],
                "summary": {
                    "checkoutSessionCount": len(checkout_sessions),
                    "paidCheckoutSessionCount": len(paid_sessions),
                    "openCheckoutSessionCount": len(open_sessions),
                    "manualOverrideCount": len(billing_overrides),
                    "latestPaymentStatus": _clean_text(
                        getattr(latest_session, "payment_status", None)
                    ),
                },
                "latestPaidSession": (
                    _checkout_session_payload(latest_paid_session) if latest_paid_session else None
                ),
                "checkoutSessions": [
                    _checkout_session_payload(session)
                    for session in checkout_sessions[:25]
                ],
                "uploads": [_upload_payload(upload) for upload in uploads],
                "billingOverrides": [
                    _billing_override_payload(override)
                    for override in billing_overrides
                ],
                "invoices": [],
                "subscriptions": [],
            }
        )
    except Exception:
        logger.exception("[admin_api] billing client lookup failed client_email=%s", client_email)
        return _error_response(500, "billing_lookup_failed", "Unable to load billing details.")
    finally:
        db.close()


@app.get("/api/admin/billing/overview")
def get_admin_billing_overview(
    request: Request,
    status: str = Query("open"),
    search: Optional[str] = None,
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    _, error_response = _require_admin_user(request)
    if error_response:
        return error_response

    normalized_status = (status or "open").strip().lower()
    if normalized_status not in {"open", "paid", "all"}:
        return _error_response(400, "invalid_status", "status must be open, paid, or all.")

    safe_limit = min(limit, 100)
    normalized_search = (search or "").strip()
    search_like = f"%{normalized_search}%" if normalized_search else None

    db = SessionLocal()
    try:
        checkout_query = db.query(StripeCheckoutSession)
        override_query = db.query(BillingOverride)
        if search_like:
            checkout_query = checkout_query.filter(
                or_(
                    StripeCheckoutSession.client_email.ilike(search_like),
                    StripeCheckoutSession.purpose.ilike(search_like),
                )
            )
            override_query = override_query.filter(BillingOverride.client_email.ilike(search_like))

        checkout_session_count = checkout_query.count()
        paid_checkout_session_count = checkout_query.filter(
            func.lower(StripeCheckoutSession.payment_status) == "paid"
        ).count()
        open_checkout_filter = or_(
            StripeCheckoutSession.payment_status.is_(None),
            func.lower(StripeCheckoutSession.payment_status) != "paid",
            func.lower(StripeCheckoutSession.status) == "open",
        )
        open_checkout_session_count = checkout_query.filter(open_checkout_filter).count()
        manual_override_count = override_query.count()
        needs_review_event_count = (
            db.query(StripeEvent)
            .filter(StripeEvent.processing_status == "needs_review")
            .count()
        )

        filtered_checkout_query = checkout_query
        if normalized_status == "paid":
            filtered_checkout_query = filtered_checkout_query.filter(
                func.lower(StripeCheckoutSession.payment_status) == "paid"
            )
        elif normalized_status == "open":
            filtered_checkout_query = filtered_checkout_query.filter(open_checkout_filter)

        checkout_rows = (
            filtered_checkout_query.order_by(StripeCheckoutSession.created_at.desc())
            .offset(offset)
            .limit(safe_limit + 1)
            .all()
        )
        has_more = len(checkout_rows) > safe_limit
        checkout_rows = checkout_rows[:safe_limit]

        override_rows = (
            override_query.order_by(BillingOverride.created_at.desc())
            .offset(offset)
            .limit(safe_limit)
            .all()
        )

        return JSONResponse(
            {
                "ok": True,
                "summary": {
                    "checkoutSessionCount": checkout_session_count,
                    "paidCheckoutSessionCount": paid_checkout_session_count,
                    "openCheckoutSessionCount": open_checkout_session_count,
                    "manualOverrideCount": manual_override_count,
                    "needsReviewEventCount": needs_review_event_count,
                },
                "checkoutSessions": [
                    _checkout_session_payload(session)
                    for session in checkout_rows
                ],
                "billingOverrides": [
                    _billing_override_payload(override)
                    for override in override_rows
                ],
                "limit": safe_limit,
                "offset": offset,
                "count": len(checkout_rows),
                "hasMore": has_more,
            }
        )
    except Exception:
        logger.exception("[admin_api] billing overview lookup failed.")
        return _error_response(500, "billing_overview_failed", "Unable to load billing overview.")
    finally:
        db.close()


@app.post("/api/admin/billing/overrides")
async def create_admin_billing_override(request: Request) -> JSONResponse:
    admin_user, error_response = _require_admin_user(request)
    if error_response:
        return error_response

    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    target_type, validation_error = _required_text(body.get("targetType"), "targetType")
    if validation_error:
        return validation_error
    target_id, validation_error = _required_text(body.get("targetId"), "targetId")
    if validation_error:
        return validation_error
    client_email, validation_error = _required_email(body.get("clientEmail"))
    if validation_error:
        return validation_error
    override_paid, validation_error = _required_bool(body.get("overridePaid"), "overridePaid")
    if validation_error:
        return validation_error
    reason, validation_error = _required_reason(body.get("reason"))
    if validation_error:
        return validation_error

    admin_user_id = str(admin_user.get("id") or "")
    db = SessionLocal()
    try:
        override = BillingOverride(
            target_type=target_type,
            target_id=target_id,
            client_email=client_email,
            override_paid=override_paid,
            reason=reason,
            admin_user_id=admin_user_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(override)
        db.commit()
        db.refresh(override)
        logger.info(
            "[admin_api] billing override recorded target_type=%s target_id=%s client_email=%s override_paid=%s admin_user_id=%s",
            target_type,
            target_id,
            client_email,
            override_paid,
            admin_user_id,
        )
        return JSONResponse(
            {
                "ok": True,
                "override": _billing_override_payload(override),
            }
        )
    except Exception:
        db.rollback()
        logger.exception(
            "[admin_api] billing override failed target_type=%s target_id=%s client_email=%s",
            target_type,
            target_id,
            client_email,
        )
        return _error_response(500, "billing_override_failed", "Unable to record billing override.")
    finally:
        db.close()


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request) -> JSONResponse:
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    if not webhook_secret:
        logger.error("[admin_api] Stripe webhook secret is not configured.")
        return _error_response(503, "stripe_not_configured", "Stripe webhook is not configured.")

    signature = request.headers.get("stripe-signature", "")
    if not signature:
        return _error_response(400, "missing_signature", "Stripe signature is required.")

    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, signature, webhook_secret)
    except ValueError:
        return _error_response(400, "invalid_payload", "Invalid Stripe webhook payload.")
    except stripe.error.SignatureVerificationError:
        return _error_response(400, "invalid_signature", "Invalid Stripe webhook signature.")
    except Exception:
        logger.exception("[admin_api] Stripe webhook verification failed.")
        return _error_response(400, "invalid_webhook", "Invalid Stripe webhook.")

    return _record_stripe_event(event, payload)


def _record_stripe_event(event: Any, payload: bytes) -> JSONResponse:
    event_data = _stripe_event_payload_to_dict(payload) or _stripe_event_to_dict(event)
    event_id = _clean_text(event_data.get("id"))
    event_type = _clean_text(event_data.get("type")) or "unknown"
    if not event_id:
        return _error_response(400, "missing_event_id", "Stripe event id is required.")

    now = datetime.now(timezone.utc)
    payload_text = payload.decode("utf-8", errors="replace")
    db = SessionLocal()
    try:
        existing_event = (
            db.query(StripeEvent)
            .filter(StripeEvent.stripe_event_id == event_id)
            .first()
        )
        if existing_event:
            if existing_event.processing_status != "processed":
                existing_event.processing_status = "duplicate"
                existing_event.processed_at = existing_event.processed_at or now
                db.commit()
            return JSONResponse({"ok": True, "received": True})

        stripe_event = StripeEvent(
            stripe_event_id=event_id,
            event_type=event_type,
            livemode=bool(event_data.get("livemode")),
            api_version=_clean_text(event_data.get("api_version")),
            processing_status="received",
            received_at=now,
            payload=payload_text,
        )
        db.add(stripe_event)
        db.flush()
        stripe_event.processing_status = _process_stripe_event(db, event_data, now)
        stripe_event.processed_at = now
        db.commit()
        return JSONResponse({"ok": True, "received": True})
    except IntegrityError:
        db.rollback()
        return JSONResponse({"ok": True, "received": True})
    except Exception:
        db.rollback()
        logger.exception(
            "[admin_api] Stripe event storage failed event_id=%s event_type=%s",
            event_id,
            event_type,
        )
        return _error_response(500, "stripe_event_storage_failed", "Unable to store Stripe event.")
    finally:
        db.close()


def _process_stripe_event(db: Any, event_data: dict[str, Any], now: datetime) -> str:
    event_type = _clean_text(event_data.get("type")) or "unknown"
    if event_type != "checkout.session.completed":
        return "processed"

    session_data = _stripe_checkout_session_from_event(event_data)
    checkout_session_id = _clean_text(session_data.get("id"))
    if not checkout_session_id:
        logger.warning("[admin_api] Stripe checkout.session.completed missing session id.")
        return "needs_review"

    local_session = (
        db.query(StripeCheckoutSession)
        .filter(StripeCheckoutSession.stripe_checkout_session_id == checkout_session_id)
        .first()
    )
    if not local_session:
        logger.warning(
            "[admin_api] Stripe checkout session not found for completed event session_id=%s",
            checkout_session_id,
        )
        return "needs_review"

    local_session.status = _clean_text(session_data.get("status")) or local_session.status
    local_session.payment_status = (
        _clean_text(session_data.get("payment_status")) or local_session.payment_status
    )
    amount_total = _optional_int(session_data.get("amount_total"))
    if amount_total is not None:
        local_session.amount_total = amount_total
    local_session.currency = _clean_text(session_data.get("currency")) or local_session.currency
    local_session.stripe_customer_id = (
        _stripe_id(session_data.get("customer")) or local_session.stripe_customer_id
    )
    session_livemode = session_data.get("livemode", event_data.get("livemode"))
    if session_livemode is not None:
        local_session.livemode = bool(session_livemode)
    local_session.updated_at = now
    logger.info(
        "[admin_api] Stripe checkout session completed session_id=%s status=%s payment_status=%s",
        checkout_session_id,
        local_session.status,
        local_session.payment_status,
    )
    return "processed"


def _stripe_checkout_session_from_event(event_data: dict[str, Any]) -> dict[str, Any]:
    data = event_data.get("data")
    if not isinstance(data, dict):
        return {}
    session_data = data.get("object")
    if isinstance(session_data, dict):
        return session_data
    return _stripe_object_to_dict(session_data)


def _stripe_id(value: object) -> Optional[str]:
    if isinstance(value, dict):
        return _clean_text(value.get("id"))
    return _clean_text(value)


def _stripe_customer_payload(customer: StripeCustomer) -> dict[str, Any]:
    return {
        "id": _id_text(getattr(customer, "id", None)),
        "userId": _id_text(getattr(customer, "user_id", None)),
        "clientEmail": _clean_text(getattr(customer, "client_email", None)),
        "stripeCustomerId": _clean_text(getattr(customer, "stripe_customer_id", None)),
        "livemode": bool(getattr(customer, "livemode", False)),
        "createdAt": _iso_datetime(getattr(customer, "created_at", None)),
        "updatedAt": _iso_datetime(getattr(customer, "updated_at", None)),
    }


def _checkout_session_payload(session: StripeCheckoutSession) -> dict[str, Any]:
    return {
        "id": _id_text(getattr(session, "id", None)),
        "stripeCheckoutSessionId": _clean_text(
            getattr(session, "stripe_checkout_session_id", None)
        ),
        "stripeCustomerId": _clean_text(getattr(session, "stripe_customer_id", None)),
        "clientEmail": _clean_text(getattr(session, "client_email", None)),
        "purpose": _clean_text(getattr(session, "purpose", None)),
        "mode": _clean_text(getattr(session, "mode", None)),
        "status": _clean_text(getattr(session, "status", None)),
        "paymentStatus": _clean_text(getattr(session, "payment_status", None)),
        "amountTotal": _optional_int(getattr(session, "amount_total", None)),
        "currency": _clean_text(getattr(session, "currency", None)),
        "checkoutUrl": _clean_text(getattr(session, "checkout_url", None)),
        "livemode": bool(getattr(session, "livemode", False)),
        "uploadId": _id_text(getattr(session, "upload_id", None)),
        "clientSubmissionId": _id_text(getattr(session, "client_submission_id", None)),
        "createdAt": _iso_datetime(getattr(session, "created_at", None)),
        "updatedAt": _iso_datetime(getattr(session, "updated_at", None)),
    }


def _upload_payload(upload: Upload) -> dict[str, Any]:
    return {
        "id": _id_text(getattr(upload, "id", None)),
        "fileName": _clean_text(getattr(upload, "file_name", None)),
        "toolName": _clean_text(getattr(upload, "tool_name", None)),
        "paid": bool(getattr(upload, "paid", False)),
        "uploadTime": _clean_text(getattr(upload, "upload_time", None)),
    }


def _billing_override_payload(override: BillingOverride) -> dict[str, Any]:
    return {
        "id": _id_text(getattr(override, "id", None)),
        "targetType": _clean_text(getattr(override, "target_type", None)),
        "targetId": _clean_text(getattr(override, "target_id", None)),
        "clientEmail": _clean_text(getattr(override, "client_email", None)),
        "overridePaid": bool(getattr(override, "override_paid", False)),
        "reason": _clean_text(getattr(override, "reason", None)),
        "adminUserId": _clean_text(getattr(override, "admin_user_id", None)),
        "createdAt": _iso_datetime(getattr(override, "created_at", None)),
    }


def _id_text(value: object) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _stripe_event_payload_to_dict(payload: bytes) -> dict[str, Any]:
    try:
        payload_data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if isinstance(payload_data, dict):
        return payload_data
    return {}


def _stripe_event_to_dict(event: Any) -> dict[str, Any]:
    return _stripe_object_to_dict(event)


def _stripe_object_to_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict_recursive"):
        converted = value.to_dict_recursive()
        if isinstance(converted, dict):
            return converted
    if isinstance(value, dict):
        return value
    return {
        key: getattr(value, key)
        for key in (
            "id",
            "url",
            "livemode",
            "mode",
            "status",
            "payment_status",
            "amount_total",
            "currency",
        )
        if hasattr(value, key)
    }


async def _request_json_body(request: Request) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    try:
        body = await request.json()
    except Exception:
        return {}, _error_response(400, "invalid_json", "Request body must be valid JSON.")
    if not isinstance(body, dict):
        return {}, _error_response(400, "invalid_json", "Request body must be a JSON object.")
    return body, None


def _required_email(value: object) -> tuple[str, Optional[JSONResponse]]:
    email = _clean_text(value)
    if not email:
        return "", _error_response(400, "missing_client_email", "clientEmail is required.")
    email = email.lower()
    if len(email) > 254 or "@" not in email:
        return "", _error_response(400, "invalid_client_email", "clientEmail must be a valid email.")
    return email, None


def _required_text(value: object, field_name: str) -> tuple[str, Optional[JSONResponse]]:
    text = _clean_text(value)
    if not text:
        return "", _error_response(400, f"missing_{field_name}", f"{field_name} is required.")
    return text, None


def _required_amount(value: object) -> tuple[int, Optional[JSONResponse]]:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0, _error_response(400, "invalid_amount", "amount must be an integer number of cents.")
    if value <= 0:
        return 0, _error_response(400, "invalid_amount", "amount must be greater than zero.")
    if value > 10000000:
        return 0, _error_response(400, "invalid_amount", "amount is too large.")
    return value, None


def _required_bool(value: object, field_name: str) -> tuple[bool, Optional[JSONResponse]]:
    if not isinstance(value, bool):
        return False, _error_response(400, f"invalid_{field_name}", f"{field_name} must be a boolean.")
    return value, None


def _required_reason(value: object) -> tuple[str, Optional[JSONResponse]]:
    reason = _clean_text(value)
    if not reason:
        return "", _error_response(400, "missing_reason", "reason is required.")
    if len(reason) < 5:
        return "", _error_response(400, "invalid_reason", "reason must be at least 5 characters.")
    if len(reason) > 2000:
        return "", _error_response(400, "invalid_reason", "reason must be 2000 characters or fewer.")
    return reason, None


def _optional_uuid(value: object, field_name: str) -> tuple[Optional[UUID], Optional[JSONResponse]]:
    text = _clean_text(value)
    if not text or text.lower() == "null":
        return None, None
    try:
        return UUID(text), None
    except ValueError:
        return None, _error_response(400, f"invalid_{field_name}", f"{field_name} must be a valid UUID.")


def _is_safe_checkout_url(value: str) -> bool:
    return value.startswith("https://") or value.startswith("http://")


def _get_or_create_stripe_customer(
    *,
    db: Any,
    client_email: str,
    user_record: Optional[User],
    stripe_secret_key: str,
) -> tuple[str, bool]:
    local_customer = (
        db.query(StripeCustomer)
        .filter(StripeCustomer.client_email == client_email)
        .filter(StripeCustomer.stripe_customer_id.isnot(None))
        .order_by(StripeCustomer.updated_at.desc())
        .first()
    )
    stripe_customer_id = _clean_text(getattr(local_customer, "stripe_customer_id", None))
    if not stripe_customer_id:
        stripe_customer_id = _clean_text(getattr(user_record, "stripe_customer_id", None))
        if stripe_customer_id:
            local_customer = (
                db.query(StripeCustomer)
                .filter(StripeCustomer.stripe_customer_id == stripe_customer_id)
                .first()
            )

    livemode = bool(getattr(local_customer, "livemode", False))
    now = datetime.now(timezone.utc)
    if not stripe_customer_id:
        stripe_customer = stripe.Customer.create(
            email=client_email,
            metadata={"source": "consulting_admin_api"},
            api_key=stripe_secret_key,
        )
        customer_data = _stripe_object_to_dict(stripe_customer)
        stripe_customer_id = _clean_text(customer_data.get("id"))
        if not stripe_customer_id:
            raise RuntimeError("Stripe customer response missing id.")
        livemode = bool(customer_data.get("livemode"))

    if local_customer:
        local_customer.client_email = client_email
        if user_record:
            local_customer.user_id = getattr(user_record, "id", None)
        local_customer.livemode = livemode
        local_customer.updated_at = now
    else:
        db.add(
            StripeCustomer(
                user_id=getattr(user_record, "id", None),
                client_email=client_email,
                stripe_customer_id=stripe_customer_id,
                livemode=livemode,
                created_at=now,
                updated_at=now,
            )
        )

    if user_record and not _clean_text(getattr(user_record, "stripe_customer_id", None)):
        user_record.stripe_customer_id = stripe_customer_id

    return stripe_customer_id, livemode


def _optional_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _require_admin_user(request: Request) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    access_token = _bearer_token(request)
    if not access_token:
        return {}, _error_response(401, "unauthorized", "Authentication is required.")

    user = get_current_admin_user(access_token)
    if not user or not user.get("id"):
        return {}, _error_response(401, "unauthorized", "Authentication is invalid.")

    user_id = str(user.get("id") or "")
    if not is_admin_user(user_id):
        logger.warning("[admin_api] non-admin access denied user_id=%s", user_id)
        return {}, _error_response(403, "forbidden", "Admin access is required.")

    return user, None


def _clients_response(
    items: list[dict[str, Any]],
    limit: int,
    offset: int,
    *,
    has_more: bool,
) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "items": items,
            "limit": limit,
            "offset": offset,
            "count": len(items),
            "hasMore": has_more,
        }
    )


def _empty_billing_summary() -> dict[str, Any]:
    return {
        "checkoutSessionCount": 0,
        "paidCheckoutSessionCount": 0,
        "openCheckoutSessionCount": 0,
        "manualOverrideCount": 0,
        "latestPaymentStatus": None,
    }


def _clean_text(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _full_name(submission: Optional[ClientSubmission]) -> Optional[str]:
    if not submission:
        return None
    first_name = _clean_text(getattr(submission, "first_name", None)) or ""
    last_name = _clean_text(getattr(submission, "last_name", None)) or ""
    full_name = f"{first_name} {last_name}".strip()
    return full_name or None


def _iso_datetime(value: object) -> Optional[str]:
    if not isinstance(value, datetime):
        return None
    return value.isoformat().replace("+00:00", "Z")


def _bearer_token(request: Request) -> Optional[str]:
    header_value = request.headers.get("authorization", "")
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error": {
                "code": code,
                "message": message,
            },
        },
    )
