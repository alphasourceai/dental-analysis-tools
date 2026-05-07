from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import stripe
from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, or_

from database import SessionLocal
from models import ClientSubmission, StripeEvent, Upload, User
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
        if client_emails:
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
                }
            )

        return _clients_response(items, safe_limit, offset, has_more=has_more)
    except Exception:
        logger.exception("[admin_api] client list query failed.")
        return _error_response(500, "internal_error", "Unable to load clients.")
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
    event_data = _stripe_event_to_dict(event)
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
        stripe_event.processing_status = "processed"
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


def _stripe_event_to_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "to_dict_recursive"):
        return event.to_dict_recursive()
    if isinstance(event, dict):
        return event
    return {}


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
