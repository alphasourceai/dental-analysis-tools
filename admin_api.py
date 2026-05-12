from __future__ import annotations

import logging
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, unquote, urlparse
from uuid import UUID, uuid4

import stripe
from fastapi import FastAPI, File, Form, Query, Request, Response, UploadFile as FastAPIUploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, or_

from admin_financial_processing_service import (
    AdminFinancialProcessingCanceled,
    AdminFinancialProcessingError,
    download_upload_file_bytes,
    extract_csv_text,
    extract_pdf_text,
    extract_xlsx_text,
    run_financial_csv_analysis,
)
from admin_pdf_generator_service import (
    cleanup_pdf_report,
    create_report_signed_url,
    generate_pdf_bytes,
    safe_path_component,
    upload_pdf_report,
)
from database import SessionLocal
from models import (
    AdminAnalysisJob,
    AdminAnalysisJobFile,
    AdminUser,
    BillingOverride,
    ClientSubmission,
    StripeCheckoutSession,
    StripeCheckoutSessionUpload,
    StripeCustomer,
    StripeEvent,
    Upload,
    UploadFile as UploadFileRecord,
    UploadPortalFile,
    User,
)
from supabase_utils import (
    _get_supabase_admin_client,
    get_current_admin_user,
    is_admin_user,
    persist_upload_file,
    resolve_admin_auth_user_by_email,
)
from upload_portal import PortalError, create_upload_request

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="Admin API")

ADMIN_ANALYSIS_ALLOWED_TOOL_NAMES = {
    "Financial Analyzer",
    "AR Analyzer",
    "Insurance Claim Analyzer",
}
ADMIN_ANALYSIS_FINANCIAL_TOOL_NAME = "Financial Analyzer"
ADMIN_ANALYSIS_FINANCIAL_ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".pdf"}
ADMIN_ANALYSIS_AR_TOOL_NAME = "AR Analyzer"
ADMIN_ANALYSIS_AR_ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".pdf"}
ADMIN_ANALYSIS_CLAIMS_TOOL_NAME = "Insurance Claim Analyzer"
ADMIN_ANALYSIS_CLAIMS_ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".pdf"}
ADMIN_ANALYSIS_READ_CHUNK_SIZE = 1024 * 1024
DEFAULT_ADMIN_ANALYSIS_MAX_FILE_MB = 15
PERMISSION_CLIENTS_READ = "clients_read"
PERMISSION_BILLING_READ = "billing_read"
PERMISSION_BILLING_WRITE = "billing_write"
PERMISSION_ANALYSIS_READ = "analysis_read"
PERMISSION_ANALYSIS_WRITE = "analysis_write"
PERMISSION_PDF_READ = "pdf_read"
PERMISSION_PDF_GENERATE = "pdf_generate"
PERMISSION_SECURE_UPLOADS_READ = "secure_uploads_read"
PERMISSION_SECURE_UPLOADS_WRITE = "secure_uploads_write"
PERMISSION_ADMIN_MANAGEMENT_READ = "admin_management_read"
PERMISSION_ADMIN_MANAGEMENT_WRITE = "admin_management_write"
ADMIN_API_DASHBOARD_ROLES = {"super_admin", "admin", "analyst", "billing_admin", "viewer"}
ADMIN_API_ALL_PERMISSIONS = {
    PERMISSION_CLIENTS_READ,
    PERMISSION_BILLING_READ,
    PERMISSION_BILLING_WRITE,
    PERMISSION_ANALYSIS_READ,
    PERMISSION_ANALYSIS_WRITE,
    PERMISSION_PDF_READ,
    PERMISSION_PDF_GENERATE,
    PERMISSION_SECURE_UPLOADS_READ,
    PERMISSION_SECURE_UPLOADS_WRITE,
    PERMISSION_ADMIN_MANAGEMENT_READ,
    PERMISSION_ADMIN_MANAGEMENT_WRITE,
}
ADMIN_ROLE_PERMISSION_MAP = {
    "super_admin": ADMIN_API_ALL_PERMISSIONS,
    "admin": ADMIN_API_ALL_PERMISSIONS - {PERMISSION_ADMIN_MANAGEMENT_WRITE},
    "analyst": {
        PERMISSION_CLIENTS_READ,
        PERMISSION_ANALYSIS_READ,
        PERMISSION_ANALYSIS_WRITE,
        PERMISSION_PDF_READ,
        PERMISSION_PDF_GENERATE,
        PERMISSION_SECURE_UPLOADS_READ,
        PERMISSION_SECURE_UPLOADS_WRITE,
    },
    "billing_admin": {
        PERMISSION_CLIENTS_READ,
        PERMISSION_BILLING_READ,
        PERMISSION_BILLING_WRITE,
    },
    "viewer": {
        PERMISSION_CLIENTS_READ,
        PERMISSION_BILLING_READ,
        PERMISSION_ANALYSIS_READ,
        PERMISSION_PDF_READ,
        PERMISSION_SECURE_UPLOADS_READ,
    },
}
ADMIN_USER_WRITE_ROLES = set(ADMIN_ROLE_PERMISSION_MAP.keys())

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
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
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
    user, admin_user, error_response = _require_dashboard_access(request)
    if error_response:
        return error_response

    admin_access = _admin_access_payload_for_user(user, admin_user)

    return JSONResponse(
        {
            "ok": True,
            "admin": admin_access,
            "permissions": _admin_permissions_payload(admin_user),
            # Compatibility fields for existing React Admin clients.
            "user": {
                "id": admin_access["id"],
                "email": admin_access["email"],
            },
            "role": admin_access["role"],
        }
    )


@app.get("/api/admin/admin-users")
def list_admin_users(request: Request) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_ADMIN_MANAGEMENT_READ)
    if error_response:
        return error_response

    db = SessionLocal()
    try:
        rows = db.query(AdminUser).order_by(AdminUser.user_id.asc()).all()
        return JSONResponse(
            {
                "ok": True,
                "items": [_admin_user_payload(row) for row in rows],
            }
        )
    except Exception:
        logger.exception("[admin_api] admin user list failed.")
        return _error_response(500, "admin_users_lookup_failed", "Unable to load admin users.")
    finally:
        db.close()


@app.post("/api/admin/admin-users")
async def create_admin_user_access(request: Request) -> JSONResponse:
    current_user, _, error_response = _require_admin_permission(request, PERMISSION_ADMIN_MANAGEMENT_WRITE)
    if error_response:
        return error_response

    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    if _clean_text(body.get("userId")):
        return _error_response(
            400,
            "unsupported_user_id",
            "Supabase Auth user ID is resolved by the backend.",
        )

    display_name, validation_error = _required_admin_display_name(body.get("name"))
    if validation_error:
        return validation_error
    email, validation_error = _required_admin_access_email(body.get("email"))
    if validation_error:
        return validation_error
    role, validation_error = _required_admin_user_role(body.get("role"))
    if validation_error:
        return validation_error

    created_by = _uuid_or_none(current_user.get("id"))
    now = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        existing_email = db.query(AdminUser).filter(func.lower(AdminUser.email) == email).first()
        if existing_email:
            return _error_response(409, "admin_user_exists", "Admin access already exists for this user.")
    except Exception:
        logger.exception("[admin_api] admin user email duplicate check failed email=%s", email)
        return _error_response(500, "admin_user_lookup_failed", "Unable to check admin access.")
    finally:
        db.close()

    auth_user, auth_error = resolve_admin_auth_user_by_email(email)
    if auth_error:
        return _error_response(
            int(auth_error.get("status") or 500),
            str(auth_error.get("code") or "supabase_auth_failed"),
            str(auth_error.get("message") or "Unable to resolve Supabase Auth user."),
        )

    target_user_id = _uuid_or_none(auth_user.get("user_id") if auth_user else None)
    if not target_user_id:
        return _error_response(
            502,
            "supabase_auth_invalid_user",
            "Supabase Auth user response was invalid.",
        )

    db = SessionLocal()
    try:
        existing_email = db.query(AdminUser).filter(func.lower(AdminUser.email) == email).first()
        existing_user = db.query(AdminUser).filter(AdminUser.user_id == target_user_id).first()
        if existing_email or existing_user:
            return _error_response(409, "admin_user_exists", "Admin access already exists for this user.")

        admin_user = AdminUser(
            user_id=target_user_id,
            display_name=display_name,
            email=email,
            role=role,
            status="active",
            created_at=now,
            updated_at=now,
            created_by=created_by,
            deactivated_at=None,
        )
        db.add(admin_user)
        db.commit()
        db.refresh(admin_user)
        return JSONResponse(
            {
                "ok": True,
                "adminUser": _admin_user_payload(admin_user),
                "auth": {
                    "existingUser": bool(auth_user.get("existing")),
                    "inviteSent": bool(auth_user.get("invited")),
                },
            }
        )
    except IntegrityError:
        db.rollback()
        return _error_response(409, "admin_user_exists", "Admin access already exists for this user.")
    except Exception:
        db.rollback()
        logger.exception(
            "[admin_api] admin user create failed target_user_id=%s invite_sent=%s",
            target_user_id,
            bool(auth_user.get("invited")) if auth_user else False,
        )
        return _error_response(500, "admin_user_create_failed", "Unable to add admin access.")
    finally:
        db.close()


@app.patch("/api/admin/admin-users/{user_id}")
async def update_admin_user_access(request: Request, user_id: str) -> JSONResponse:
    current_user, _, error_response = _require_admin_permission(request, PERMISSION_ADMIN_MANAGEMENT_WRITE)
    if error_response:
        return error_response

    target_user_id, validation_error = _required_uuid(user_id, "userId")
    if validation_error:
        return validation_error

    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    has_role_update = "role" in body
    has_status_update = "status" in body
    has_name_update = "name" in body
    if not has_role_update and not has_status_update and not has_name_update:
        return _error_response(
            400,
            "missing_admin_user_update",
            "At least one of role, status, or name is required.",
        )

    next_role = None
    if has_role_update:
        next_role, validation_error = _required_admin_user_role(body.get("role"))
        if validation_error:
            return validation_error

    next_status = None
    if has_status_update:
        next_status, validation_error = _required_admin_user_status(body.get("status"))
        if validation_error:
            return validation_error

    next_display_name = None
    if has_name_update:
        next_display_name, validation_error = _optional_admin_display_name(body.get("name"))
        if validation_error:
            return validation_error

    current_user_id = _uuid_or_none(current_user.get("id"))
    now = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        admin_user = db.query(AdminUser).filter(AdminUser.user_id == target_user_id).first()
        if not admin_user:
            return _error_response(404, "admin_user_not_found", "Admin access was not found.")

        is_self_update = current_user_id == target_user_id
        current_role = (_clean_text(getattr(admin_user, "role", None)) or "").lower()
        current_status = (_clean_text(getattr(admin_user, "status", None)) or "active").lower()
        currently_active_super_admin = (
            current_role == "super_admin"
            and current_status == "active"
            and not getattr(admin_user, "deactivated_at", None)
        )

        if is_self_update and current_role == "super_admin" and next_role and next_role != "super_admin":
            return _error_response(400, "self_demotion_blocked", "You cannot change your own super admin role.")
        if is_self_update and next_status == "inactive":
            return _error_response(400, "self_deactivation_blocked", "You cannot deactivate your own admin access.")

        final_role = next_role or current_role
        final_status = next_status or current_status or "active"
        final_active_super_admin = final_role == "super_admin" and final_status == "active"
        if currently_active_super_admin and not final_active_super_admin and _active_super_admin_count(db) <= 1:
            return _error_response(
                400,
                "last_super_admin_blocked",
                "At least one active super admin is required.",
            )

        if next_role:
            admin_user.role = next_role
        if next_status == "inactive":
            admin_user.status = "inactive"
            admin_user.deactivated_at = now
        elif next_status == "active":
            admin_user.status = "active"
            admin_user.deactivated_at = None
        if has_name_update:
            admin_user.display_name = next_display_name
        admin_user.updated_at = now

        db.commit()
        db.refresh(admin_user)
        return JSONResponse(
            {
                "ok": True,
                "adminUser": _admin_user_payload(admin_user),
            }
        )
    except Exception:
        db.rollback()
        logger.exception("[admin_api] admin user update failed target_user_id=%s", target_user_id)
        return _error_response(500, "admin_user_update_failed", "Unable to update admin access.")
    finally:
        db.close()


@app.get("/api/admin/clients")
def list_admin_clients(
    request: Request,
    search: Optional[str] = None,
    limit: int = Query(25, ge=1),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    _, admin_access, error_response = _require_admin_permission(request, PERMISSION_CLIENTS_READ)
    if error_response:
        return error_response
    can_read_billing = _admin_has_permission(admin_access, PERMISSION_BILLING_READ)

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
            if can_read_billing:
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


@app.get("/api/admin/client-options")
def list_admin_client_options(
    request: Request,
    search: Optional[str] = None,
    limit: int = Query(75, ge=1),
) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_CLIENTS_READ)
    if error_response:
        return error_response

    safe_limit = min(limit, 100)
    normalized_search = (search or "").strip()
    db = SessionLocal()
    try:
        candidates: dict[str, dict[str, Any]] = {}

        submission_query = db.query(ClientSubmission)
        user_query = db.query(User)
        if normalized_search:
            search_like = f"%{normalized_search}%"
            submission_query = submission_query.filter(
                or_(
                    ClientSubmission.user_email.ilike(search_like),
                    ClientSubmission.first_name.ilike(search_like),
                    ClientSubmission.last_name.ilike(search_like),
                    ClientSubmission.office_name.ilike(search_like),
                    ClientSubmission.phone.ilike(search_like),
                )
            )
            user_query = user_query.filter(
                or_(
                    User.email.ilike(search_like),
                    User.first_name.ilike(search_like),
                    User.last_name.ilike(search_like),
                    User.office_name.ilike(search_like),
                    User.phone.ilike(search_like),
                )
            )

        submissions = (
            submission_query.order_by(ClientSubmission.submitted_at.desc())
            .limit(safe_limit)
            .all()
        )
        for submission in submissions:
            _merge_client_option_submission(candidates, submission)

        users = user_query.order_by(User.email.asc()).limit(safe_limit).all()
        for user in users:
            _merge_client_option_user(candidates, user)

        candidate_emails = list(candidates.keys())
        if candidate_emails:
            latest_submissions = (
                db.query(ClientSubmission)
                .filter(func.lower(ClientSubmission.user_email).in_(candidate_emails))
                .order_by(
                    func.lower(ClientSubmission.user_email).asc(),
                    ClientSubmission.submitted_at.desc(),
                )
                .all()
            )
            seen_latest_emails: set[str] = set()
            for submission in latest_submissions:
                email = (_clean_text(getattr(submission, "user_email", None)) or "").lower()
                if not email or email in seen_latest_emails:
                    continue
                seen_latest_emails.add(email)
                _merge_client_option_submission(candidates, submission)

        items = sorted(
            candidates.values(),
            key=lambda candidate: (
                _datetime_timestamp(candidate.get("_latestSubmittedAt")),
                candidate.get("email") or "",
            ),
            reverse=True,
        )[:safe_limit]

        return JSONResponse(
            {
                "ok": True,
                "items": [_client_option_payload(item) for item in items],
                "limit": safe_limit,
                "count": len(items),
            }
        )
    except Exception:
        logger.exception("[admin_api] client options query failed.")
        return _error_response(500, "client_options_failed", "Unable to load client options.")
    finally:
        db.close()


@app.post("/api/admin/analysis-jobs")
async def create_admin_analysis_job(request: Request) -> JSONResponse:
    admin_user, _, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
    if error_response:
        return error_response

    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    client_email, validation_error = _required_email(body.get("clientEmail"))
    if validation_error:
        return validation_error
    first_name, validation_error = _required_text(body.get("firstName"), "firstName")
    if validation_error:
        return validation_error
    last_name, validation_error = _required_text(body.get("lastName"), "lastName")
    if validation_error:
        return validation_error
    office_name, validation_error = _required_text(body.get("officeName"), "officeName")
    if validation_error:
        return validation_error
    org_type, validation_error = _required_text(body.get("orgType"), "orgType")
    if validation_error:
        return validation_error

    file_inputs, validation_error = _validate_admin_analysis_file_inputs(body.get("files"))
    if validation_error:
        return validation_error

    now = datetime.now(timezone.utc)
    analysis_run_id = str(uuid4())
    db = SessionLocal()
    try:
        job = AdminAnalysisJob(
            status="queued",
            created_by_admin_user_id=str(admin_user.get("id") or ""),
            client_email=client_email,
            first_name=first_name,
            last_name=last_name,
            office_name=office_name,
            org_type=org_type,
            phone=_clean_text(body.get("phone")),
            ghl_cid=_clean_text(body.get("ghlCid")),
            client_mode=_clean_text(body.get("clientMode")),
            analysis_run_id=analysis_run_id,
            progress_percent=0,
            current_step="Queued",
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.flush()

        job_files = [
            AdminAnalysisJobFile(
                job_id=job.id,
                tool_name=file_input["tool_name"],
                original_filename=file_input["original_filename"],
                content_type=file_input["content_type"],
                byte_size=file_input["byte_size"],
                status="queued",
                created_at=now,
            )
            for file_input in file_inputs
        ]
        db.add_all(job_files)
        db.commit()
        db.refresh(job)
        for job_file in job_files:
            db.refresh(job_file)

        logger.info(
            "[admin_api] admin analysis job queued job_id=%s client_email=%s file_count=%s admin_user_id=%s",
            job.id,
            client_email,
            len(job_files),
            str(admin_user.get("id") or ""),
        )
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "job": _admin_analysis_job_payload(job, job_files),
            },
        )
    except Exception:
        db.rollback()
        logger.exception("[admin_api] admin analysis job create failed client_email=%s", client_email)
        return _error_response(500, "analysis_job_create_failed", "Unable to create analysis job.")
    finally:
        db.close()


@app.post("/api/admin/analysis-jobs/financial-intake")
async def create_admin_financial_intake_job(
    request: Request,
    client_mode: Optional[str] = Form(None, alias="clientMode"),
    client_email_value: Optional[str] = Form(None, alias="clientEmail"),
    first_name_value: Optional[str] = Form(None, alias="firstName"),
    last_name_value: Optional[str] = Form(None, alias="lastName"),
    office_name_value: Optional[str] = Form(None, alias="officeName"),
    org_type_value: Optional[str] = Form(None, alias="orgType"),
    phone_value: Optional[str] = Form(None, alias="phone"),
    ghl_cid_value: Optional[str] = Form(None, alias="ghlCid"),
    financial_file: Optional[FastAPIUploadFile] = File(None, alias="financialFile"),
) -> JSONResponse:
    admin_user, _, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
    if error_response:
        return error_response

    client_email, validation_error = _required_email(client_email_value)
    if validation_error:
        return validation_error
    first_name, validation_error = _required_text(first_name_value, "firstName")
    if validation_error:
        return validation_error
    last_name, validation_error = _required_text(last_name_value, "lastName")
    if validation_error:
        return validation_error
    office_name, validation_error = _required_text(office_name_value, "officeName")
    if validation_error:
        return validation_error
    org_type, validation_error = _required_text(org_type_value, "orgType")
    if validation_error:
        return validation_error
    if financial_file is None:
        return _error_response(400, "missing_financial_file", "financialFile is required.")

    single_file_error = await _validate_single_file_field(request, "financialFile")
    if single_file_error:
        await financial_file.close()
        return single_file_error

    original_filename = _clean_text(financial_file.filename)
    if not original_filename:
        await financial_file.close()
        return _error_response(400, "missing_filename", "Uploaded file name is required.")

    extension = _file_extension(original_filename)
    if extension not in ADMIN_ANALYSIS_FINANCIAL_ALLOWED_EXTENSIONS:
        await financial_file.close()
        return _error_response(400, "unsupported_file_type", "Unsupported financial file type.")

    try:
        file_bytes, file_error = await _read_admin_upload_file(financial_file)
    finally:
        await financial_file.close()
    if file_error:
        return file_error

    content_type = _clean_text(financial_file.content_type)
    now = datetime.now(timezone.utc)
    analysis_run_id = str(uuid4())
    db = SessionLocal()
    job_id: Optional[object] = None
    job_file_id: Optional[object] = None
    try:
        job = AdminAnalysisJob(
            status="intake_pending",
            created_by_admin_user_id=str(admin_user.get("id") or ""),
            client_email=client_email,
            first_name=first_name,
            last_name=last_name,
            office_name=office_name,
            org_type=org_type,
            phone=_clean_text(phone_value),
            ghl_cid=_clean_text(ghl_cid_value),
            client_mode=_clean_text(client_mode),
            analysis_run_id=analysis_run_id,
            progress_percent=0,
            current_step="Financial file intake pending",
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.flush()
        job_id = job.id

        job_file = AdminAnalysisJobFile(
            job_id=job.id,
            tool_name=ADMIN_ANALYSIS_FINANCIAL_TOOL_NAME,
            original_filename=original_filename,
            content_type=content_type,
            byte_size=len(file_bytes),
            status="intake_pending",
            created_at=now,
        )
        db.add(job_file)
        db.commit()
        db.refresh(job)
        db.refresh(job_file)
        job_file_id = job_file.id
    except Exception:
        db.rollback()
        logger.exception("[admin_api] admin financial intake job create failed client_email=%s", client_email)
        db.close()
        return _error_response(500, "analysis_job_create_failed", "Unable to create analysis job.")

    # persist_upload_file commits UploadFile separately, so the durable intake
    # record is created first and marked failed if storage cannot be linked.
    try:
        upload_file_id = persist_upload_file(
            file_bytes=file_bytes,
            user_email=client_email,
            tool_name=ADMIN_ANALYSIS_FINANCIAL_TOOL_NAME,
            original_filename=original_filename,
            content_type=content_type,
        )
    except Exception:
        logger.exception("[admin_api] admin financial intake storage raised job_id=%s", job_id)
        upload_file_id = None
    if not upload_file_id:
        _mark_admin_financial_intake_failed(
            job_id=job_id,
            job_file_id=job_file_id,
            error_code="storage_failed",
            error_message="Unable to persist uploaded file.",
        )
        logger.error(
            "[admin_api] admin financial intake storage failed job_id=%s client_email=%s filename=%s",
            job_id,
            client_email,
            original_filename,
        )
        db.close()
        return _error_response(500, "storage_failed", "Unable to persist uploaded file.")

    try:
        job.status = "queued"
        job.progress_percent = 10
        job.current_step = "Financial file received"
        job.updated_at = datetime.now(timezone.utc)
        job_file.status = "queued"
        job_file.upload_file_id = upload_file_id
        db.commit()
        db.refresh(job)
        db.refresh(job_file)
        logger.info(
            "[admin_api] admin financial intake queued job_id=%s client_email=%s upload_file_id=%s admin_user_id=%s",
            job.id,
            client_email,
            upload_file_id,
            str(admin_user.get("id") or ""),
        )
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "job": _admin_analysis_job_payload(job, [job_file]),
            },
        )
    except Exception:
        db.rollback()
        _cleanup_admin_intake_upload_file(upload_file_id)
        _mark_admin_financial_intake_failed(
            job_id=job_id,
            job_file_id=job_file_id,
            error_code="analysis_job_link_failed",
            error_message="Unable to link persisted file to analysis job.",
        )
        logger.exception("[admin_api] admin financial intake file link failed job_id=%s", job_id)
        return _error_response(500, "analysis_job_link_failed", "Unable to create analysis job.")
    finally:
        db.close()


@app.post("/api/admin/analysis-jobs/ar-intake")
async def create_admin_ar_intake_job(
    request: Request,
    client_mode: Optional[str] = Form(None, alias="clientMode"),
    client_email_value: Optional[str] = Form(None, alias="clientEmail"),
    first_name_value: Optional[str] = Form(None, alias="firstName"),
    last_name_value: Optional[str] = Form(None, alias="lastName"),
    office_name_value: Optional[str] = Form(None, alias="officeName"),
    org_type_value: Optional[str] = Form(None, alias="orgType"),
    phone_value: Optional[str] = Form(None, alias="phone"),
    ghl_cid_value: Optional[str] = Form(None, alias="ghlCid"),
    ar_file: Optional[FastAPIUploadFile] = File(None, alias="arFile"),
) -> JSONResponse:
    admin_user, _, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
    if error_response:
        return error_response

    client_email, validation_error = _required_email(client_email_value)
    if validation_error:
        return validation_error
    first_name, validation_error = _required_text(first_name_value, "firstName")
    if validation_error:
        return validation_error
    last_name, validation_error = _required_text(last_name_value, "lastName")
    if validation_error:
        return validation_error
    office_name, validation_error = _required_text(office_name_value, "officeName")
    if validation_error:
        return validation_error
    org_type, validation_error = _required_text(org_type_value, "orgType")
    if validation_error:
        return validation_error
    if ar_file is None:
        return _error_response(400, "missing_ar_file", "arFile is required.")

    single_file_error = await _validate_single_file_field(request, "arFile")
    if single_file_error:
        await ar_file.close()
        return single_file_error

    original_filename = _clean_text(ar_file.filename)
    if not original_filename:
        await ar_file.close()
        return _error_response(400, "missing_filename", "Uploaded file name is required.")

    extension = _file_extension(original_filename)
    if extension not in ADMIN_ANALYSIS_AR_ALLOWED_EXTENSIONS:
        await ar_file.close()
        return _error_response(400, "unsupported_file_type", "Unsupported AR file type.")

    try:
        file_bytes, file_error = await _read_admin_upload_file(ar_file)
    finally:
        await ar_file.close()
    if file_error:
        return file_error

    content_type = _clean_text(ar_file.content_type)
    now = datetime.now(timezone.utc)
    analysis_run_id = str(uuid4())
    db = SessionLocal()
    job_id: Optional[object] = None
    job_file_id: Optional[object] = None
    try:
        job = AdminAnalysisJob(
            status="intake_pending",
            created_by_admin_user_id=str(admin_user.get("id") or ""),
            client_email=client_email,
            first_name=first_name,
            last_name=last_name,
            office_name=office_name,
            org_type=org_type,
            phone=_clean_text(phone_value),
            ghl_cid=_clean_text(ghl_cid_value),
            client_mode=_clean_text(client_mode),
            analysis_run_id=analysis_run_id,
            progress_percent=0,
            current_step="AR file intake pending",
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.flush()
        job_id = job.id

        job_file = AdminAnalysisJobFile(
            job_id=job.id,
            tool_name=ADMIN_ANALYSIS_AR_TOOL_NAME,
            original_filename=original_filename,
            content_type=content_type,
            byte_size=len(file_bytes),
            status="intake_pending",
            created_at=now,
        )
        db.add(job_file)
        db.commit()
        db.refresh(job)
        db.refresh(job_file)
        job_file_id = job_file.id
    except Exception:
        db.rollback()
        logger.exception("[admin_api] admin AR intake job create failed client_email=%s", client_email)
        db.close()
        return _error_response(500, "analysis_job_create_failed", "Unable to create analysis job.")

    try:
        upload_file_id = persist_upload_file(
            file_bytes=file_bytes,
            user_email=client_email,
            tool_name=ADMIN_ANALYSIS_AR_TOOL_NAME,
            original_filename=original_filename,
            content_type=content_type,
        )
    except Exception:
        logger.exception("[admin_api] admin AR intake storage raised job_id=%s", job_id)
        upload_file_id = None
    if not upload_file_id:
        _mark_admin_ar_intake_failed(
            job_id=job_id,
            job_file_id=job_file_id,
            error_code="storage_failed",
            error_message="Unable to persist uploaded file.",
        )
        logger.error(
            "[admin_api] admin AR intake storage failed job_id=%s client_email=%s filename=%s",
            job_id,
            client_email,
            original_filename,
        )
        db.close()
        return _error_response(500, "storage_failed", "Unable to persist uploaded file.")

    try:
        job.status = "queued"
        job.progress_percent = 10
        job.current_step = "AR file received"
        job.updated_at = datetime.now(timezone.utc)
        job_file.status = "queued"
        job_file.upload_file_id = upload_file_id
        db.commit()
        db.refresh(job)
        db.refresh(job_file)
        logger.info(
            "[admin_api] admin AR intake queued job_id=%s client_email=%s upload_file_id=%s admin_user_id=%s",
            job.id,
            client_email,
            upload_file_id,
            str(admin_user.get("id") or ""),
        )
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "job": _admin_analysis_job_payload(job, [job_file]),
            },
        )
    except Exception:
        db.rollback()
        _cleanup_admin_intake_upload_file(upload_file_id)
        _mark_admin_ar_intake_failed(
            job_id=job_id,
            job_file_id=job_file_id,
            error_code="analysis_job_link_failed",
            error_message="Unable to link persisted file to analysis job.",
        )
        logger.exception("[admin_api] admin AR intake file link failed job_id=%s", job_id)
        return _error_response(500, "analysis_job_link_failed", "Unable to create analysis job.")
    finally:
        db.close()


@app.post("/api/admin/analysis-jobs/claims-intake")
async def create_admin_claims_intake_job(
    request: Request,
    client_mode: Optional[str] = Form(None, alias="clientMode"),
    client_email_value: Optional[str] = Form(None, alias="clientEmail"),
    first_name_value: Optional[str] = Form(None, alias="firstName"),
    last_name_value: Optional[str] = Form(None, alias="lastName"),
    office_name_value: Optional[str] = Form(None, alias="officeName"),
    org_type_value: Optional[str] = Form(None, alias="orgType"),
    phone_value: Optional[str] = Form(None, alias="phone"),
    ghl_cid_value: Optional[str] = Form(None, alias="ghlCid"),
    claims_file: Optional[FastAPIUploadFile] = File(None, alias="claimsFile"),
) -> JSONResponse:
    admin_user, _, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
    if error_response:
        return error_response

    client_email, validation_error = _required_email(client_email_value)
    if validation_error:
        return validation_error
    first_name, validation_error = _required_text(first_name_value, "firstName")
    if validation_error:
        return validation_error
    last_name, validation_error = _required_text(last_name_value, "lastName")
    if validation_error:
        return validation_error
    office_name, validation_error = _required_text(office_name_value, "officeName")
    if validation_error:
        return validation_error
    org_type, validation_error = _required_text(org_type_value, "orgType")
    if validation_error:
        return validation_error
    if claims_file is None:
        return _error_response(400, "missing_claims_file", "claimsFile is required.")

    single_file_error = await _validate_single_file_field(request, "claimsFile")
    if single_file_error:
        await claims_file.close()
        return single_file_error

    original_filename = _clean_text(claims_file.filename)
    if not original_filename:
        await claims_file.close()
        return _error_response(400, "missing_filename", "Uploaded file name is required.")

    extension = _file_extension(original_filename)
    if extension not in ADMIN_ANALYSIS_CLAIMS_ALLOWED_EXTENSIONS:
        await claims_file.close()
        return _error_response(400, "unsupported_file_type", "Unsupported Claims file type.")

    try:
        file_bytes, file_error = await _read_admin_upload_file(claims_file)
    finally:
        await claims_file.close()
    if file_error:
        return file_error

    content_type = _clean_text(claims_file.content_type)
    now = datetime.now(timezone.utc)
    analysis_run_id = str(uuid4())
    db = SessionLocal()
    job_id: Optional[object] = None
    job_file_id: Optional[object] = None
    try:
        job = AdminAnalysisJob(
            status="intake_pending",
            created_by_admin_user_id=str(admin_user.get("id") or ""),
            client_email=client_email,
            first_name=first_name,
            last_name=last_name,
            office_name=office_name,
            org_type=org_type,
            phone=_clean_text(phone_value),
            ghl_cid=_clean_text(ghl_cid_value),
            client_mode=_clean_text(client_mode),
            analysis_run_id=analysis_run_id,
            progress_percent=0,
            current_step="Claims file intake pending",
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.flush()
        job_id = job.id

        job_file = AdminAnalysisJobFile(
            job_id=job.id,
            tool_name=ADMIN_ANALYSIS_CLAIMS_TOOL_NAME,
            original_filename=original_filename,
            content_type=content_type,
            byte_size=len(file_bytes),
            status="intake_pending",
            created_at=now,
        )
        db.add(job_file)
        db.commit()
        db.refresh(job)
        db.refresh(job_file)
        job_file_id = job_file.id
    except Exception:
        db.rollback()
        logger.exception("[admin_api] admin Claims intake job create failed client_email=%s", client_email)
        db.close()
        return _error_response(500, "analysis_job_create_failed", "Unable to create analysis job.")

    try:
        upload_file_id = persist_upload_file(
            file_bytes=file_bytes,
            user_email=client_email,
            tool_name=ADMIN_ANALYSIS_CLAIMS_TOOL_NAME,
            original_filename=original_filename,
            content_type=content_type,
        )
    except Exception:
        logger.exception("[admin_api] admin Claims intake storage raised job_id=%s", job_id)
        upload_file_id = None
    if not upload_file_id:
        _mark_admin_claims_intake_failed(
            job_id=job_id,
            job_file_id=job_file_id,
            error_code="storage_failed",
            error_message="Unable to persist uploaded file.",
        )
        logger.error(
            "[admin_api] admin Claims intake storage failed job_id=%s client_email=%s filename=%s",
            job_id,
            client_email,
            original_filename,
        )
        db.close()
        return _error_response(500, "storage_failed", "Unable to persist uploaded file.")

    try:
        job.status = "queued"
        job.progress_percent = 10
        job.current_step = "Claims file received"
        job.updated_at = datetime.now(timezone.utc)
        job_file.status = "queued"
        job_file.upload_file_id = upload_file_id
        db.commit()
        db.refresh(job)
        db.refresh(job_file)
        logger.info(
            "[admin_api] admin Claims intake queued job_id=%s client_email=%s upload_file_id=%s admin_user_id=%s",
            job.id,
            client_email,
            upload_file_id,
            str(admin_user.get("id") or ""),
        )
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "job": _admin_analysis_job_payload(job, [job_file]),
            },
        )
    except Exception:
        db.rollback()
        _cleanup_admin_intake_upload_file(upload_file_id)
        _mark_admin_claims_intake_failed(
            job_id=job_id,
            job_file_id=job_file_id,
            error_code="analysis_job_link_failed",
            error_message="Unable to link persisted file to analysis job.",
        )
        logger.exception("[admin_api] admin Claims intake file link failed job_id=%s", job_id)
        return _error_response(500, "analysis_job_link_failed", "Unable to create analysis job.")
    finally:
        db.close()


@app.get("/api/admin/analysis-jobs/{job_id}")
def get_admin_analysis_job(request: Request, job_id: str) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_READ)
    if error_response:
        return error_response

    job_uuid, validation_error = _job_uuid_or_not_found(job_id)
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_uuid).first()
        if not job:
            return _error_response(404, "not_found", "Analysis job was not found.")
        files = _admin_analysis_job_files(db, job.id)
        return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})
    except Exception:
        logger.exception("[admin_api] admin analysis job lookup failed job_id=%s", job_id)
        return _error_response(500, "analysis_job_lookup_failed", "Unable to load analysis job.")
    finally:
        db.close()


@app.post("/api/admin/analysis-jobs/{job_id}/cancel")
def cancel_admin_analysis_job(request: Request, job_id: str) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
    if error_response:
        return error_response

    job_uuid, validation_error = _job_uuid_or_not_found(job_id)
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_uuid).first()
        if not job:
            return _error_response(404, "not_found", "Analysis job was not found.")

        status = (_clean_text(getattr(job, "status", None)) or "").lower()
        if status in {"queued", "processing"}:
            job.status = "cancel_requested"
            job.current_step = "Cancel requested"
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(job)

        files = _admin_analysis_job_files(db, job.id)
        return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})
    except Exception:
        db.rollback()
        logger.exception("[admin_api] admin analysis job cancel failed job_id=%s", job_id)
        return _error_response(500, "analysis_job_cancel_failed", "Unable to cancel analysis job.")
    finally:
        db.close()


@app.post("/api/admin/analysis-jobs/{job_id}/process-financial")
def process_admin_financial_analysis_job(request: Request, job_id: str) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
    if error_response:
        return error_response

    job_uuid, validation_error = _job_uuid_or_not_found(job_id)
    if validation_error:
        return validation_error

    db = SessionLocal()
    job_file_id: Optional[object] = None
    bucket: Optional[str] = None
    object_path: Optional[str] = None
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_uuid).first()
        if not job:
            return _error_response(404, "not_found", "Analysis job was not found.")

        files = _admin_analysis_job_files(db, job.id)
        status = (_clean_text(getattr(job, "status", None)) or "").lower()
        if status == "completed":
            return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})
        if status == "cancel_requested":
            _set_admin_analysis_job_canceled(db, job, files)
            db.commit()
            db.refresh(job)
            files = _admin_analysis_job_files(db, job.id)
            return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})

        financial_files = [
            file_record
            for file_record in files
            if _clean_text(getattr(file_record, "tool_name", None)) == ADMIN_ANALYSIS_FINANCIAL_TOOL_NAME
        ]
        if len(financial_files) != 1:
            return _error_response(
                409,
                "invalid_financial_job_files",
                "Analysis job must have exactly one Financial Analyzer file.",
            )

        job_file = financial_files[0]
        retrying_storage_download = False
        if status == "error":
            job_error_code = _clean_text(getattr(job, "error_code", None))
            job_file_error_code = _clean_text(getattr(job_file, "error_code", None))
            job_file_has_output = bool(
                _clean_text(getattr(job_file, "analysis_data", None))
                or getattr(job_file, "upload_id", None)
            )
            retrying_storage_download = (
                job_error_code == "storage_download_failed"
                and (
                    job_file_error_code == "storage_download_failed"
                    or not job_file_has_output
                )
            )
            if not retrying_storage_download:
                return _error_response(
                    409,
                    "invalid_job_status",
                    "Only storage download failures can be retried from error status.",
                )
        elif status not in {"queued", "processing"}:
            return _error_response(
                409,
                "invalid_job_status",
                "Analysis job must be queued or processing.",
            )

        job_file_id = job_file.id
        upload_file_id = getattr(job_file, "upload_file_id", None)
        if not upload_file_id:
            return _error_response(
                409,
                "missing_upload_file",
                "Financial Analyzer file has not been persisted.",
            )

        upload_file = (
            db.query(UploadFileRecord)
            .filter(UploadFileRecord.id == upload_file_id)
            .first()
        )
        if not upload_file:
            return _error_response(
                409,
                "missing_upload_file",
                "Persisted financial file was not found.",
            )

        original_filename = (
            _clean_text(getattr(upload_file, "original_filename", None))
            or _clean_text(getattr(job_file, "original_filename", None))
            or ""
        )
        file_extension = _file_extension(original_filename)
        if file_extension == ".pdf":
            return _error_response(
                400,
                "unsupported_file_type",
                "PDF processing will be added later.",
            )
        if file_extension not in {".csv", ".xlsx"}:
            return _error_response(
                400,
                "unsupported_file_type",
                "Unsupported financial file type.",
            )

        bucket = _clean_text(getattr(upload_file, "bucket", None))
        object_path = _clean_text(getattr(upload_file, "object_path", None))
        now = datetime.now(timezone.utc)
        job.status = "processing"
        job.progress_percent = max(_optional_int(getattr(job, "progress_percent", None)) or 0, 20)
        job.current_step = (
            "Retrying financial file download"
            if retrying_storage_download
            else "Downloading financial file"
        )
        if not getattr(job, "started_at", None):
            job.started_at = now
        job.updated_at = now
        job.error_code = None
        job.error_message = None
        job.errored_at = None

        job_file.status = "processing"
        if not getattr(job_file, "started_at", None):
            job_file.started_at = now
        job_file.error_code = None
        job_file.error_message = None
        job_file.errored_at = None
        db.commit()

        logger.info("[admin_analysis] financial processing started job_id=%s", job_uuid)
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] financial processing setup failed job_id=%s", job_id)
        return _error_response(500, "analysis_processing_failed", "Unable to process analysis job.")
    finally:
        db.close()

    if _admin_analysis_job_cancel_requested(job_uuid):
        return _cancel_admin_analysis_job_response(job_uuid)

    try:
        file_bytes = download_upload_file_bytes(bucket or "", object_path or "")
    except AdminFinancialProcessingError as exc:
        _mark_admin_financial_processing_error(job_uuid, job_file_id, exc.code, exc.message)
        return _error_response(500, exc.code, exc.message)
    except Exception:
        logger.exception("[admin_analysis] financial file download failed job_id=%s", job_uuid)
        _mark_admin_financial_processing_error(
            job_uuid,
            job_file_id,
            "storage_download_failed",
            "Unable to download stored financial file.",
        )
        return _error_response(500, "storage_download_failed", "Unable to download stored financial file.")

    _update_admin_financial_processing_progress(
        job_uuid,
        job_file_id,
        35,
        "Financial file downloaded",
    )
    if _admin_analysis_job_cancel_requested(job_uuid):
        return _cancel_admin_analysis_job_response(job_uuid)

    try:
        _update_admin_financial_processing_progress(
            job_uuid,
            job_file_id,
            45,
            f"Extracting {file_extension.lstrip('.').upper()} data",
        )
        if file_extension == ".csv":
            data_input = extract_csv_text(file_bytes)
            source_format = "csv"
        else:
            data_input = extract_xlsx_text(file_bytes)
            source_format = "xlsx"
    except AdminFinancialProcessingError as exc:
        _mark_admin_financial_processing_error(job_uuid, job_file_id, exc.code, exc.message)
        return _error_response(400, exc.code, exc.message)
    except Exception:
        logger.exception(
            "[admin_analysis] financial file extraction failed job_id=%s extension=%s",
            job_uuid,
            file_extension,
        )
        if file_extension == ".csv":
            error_code = "csv_extract_failed"
            error_message = "Unable to extract CSV data."
        else:
            error_code = "xlsx_extract_failed"
            error_message = "Unable to extract XLSX data."
        _mark_admin_financial_processing_error(
            job_uuid,
            job_file_id,
            error_code,
            error_message,
        )
        return _error_response(400, error_code, error_message)

    _update_admin_financial_processing_progress(
        job_uuid,
        job_file_id,
        60,
        "Running model analysis",
    )
    if _admin_analysis_job_cancel_requested(job_uuid):
        return _cancel_admin_analysis_job_response(job_uuid)

    try:
        analysis_data = run_financial_csv_analysis(
            data_input,
            cancel_checker=lambda: _admin_analysis_job_cancel_requested(job_uuid),
            source_format=source_format,
        )
    except AdminFinancialProcessingCanceled:
        return _cancel_admin_analysis_job_response(job_uuid)
    except AdminFinancialProcessingError as exc:
        _mark_admin_financial_processing_error(job_uuid, job_file_id, exc.code, exc.message)
        return _error_response(502, exc.code, exc.message)
    except Exception:
        logger.exception("[admin_analysis] financial model analysis failed job_id=%s", job_uuid)
        _mark_admin_financial_processing_error(
            job_uuid,
            job_file_id,
            "provider_unavailable",
            "The analyzer providers are temporarily unavailable. Please try again later.",
        )
        return _error_response(
            502,
            "provider_unavailable",
            "The analyzer providers are temporarily unavailable. Please try again later.",
        )

    if _admin_analysis_job_cancel_requested(job_uuid):
        return _cancel_admin_analysis_job_response(job_uuid)

    db = SessionLocal()
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_uuid).first()
        if not job:
            return _error_response(404, "not_found", "Analysis job was not found.")
        files = _admin_analysis_job_files(db, job.id)
        status = (_clean_text(getattr(job, "status", None)) or "").lower()
        if status == "cancel_requested":
            _set_admin_analysis_job_canceled(db, job, files)
            db.commit()
            db.refresh(job)
            files = _admin_analysis_job_files(db, job.id)
            return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})

        job_file = (
            db.query(AdminAnalysisJobFile)
            .filter(AdminAnalysisJobFile.id == job_file_id)
            .first()
        )
        if not job_file:
            return _error_response(404, "not_found", "Analysis job file was not found.")

        now = datetime.now(timezone.utc)
        job.status = "completed"
        job.progress_percent = 100
        job.current_step = "Financial analysis processed"
        job.completed_at = now
        job.updated_at = now
        job.error_code = None
        job.error_message = None

        job_file.status = "completed"
        job_file.analysis_data = json.dumps(analysis_data)
        job_file.completed_at = now
        job_file.processed_at = now
        job_file.error_code = None
        job_file.error_message = None
        db.commit()
        db.refresh(job)
        files = _admin_analysis_job_files(db, job.id)
        logger.info("[admin_analysis] financial processing completed job_id=%s", job_uuid)
        return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] financial processing final write failed job_id=%s", job_uuid)
        _mark_admin_financial_processing_error(
            job_uuid,
            job_file_id,
            "analysis_result_store_failed",
            "Unable to store financial analysis results.",
        )
        return _error_response(
            500,
            "analysis_result_store_failed",
            "Unable to store financial analysis results.",
        )
    finally:
        db.close()


@app.post("/api/admin/analysis-jobs/{job_id}/process-ar")
def process_admin_ar_analysis_job(request: Request, job_id: str) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
    if error_response:
        return error_response

    job_uuid, validation_error = _job_uuid_or_not_found(job_id)
    if validation_error:
        return validation_error

    db = SessionLocal()
    job_file_id: Optional[object] = None
    bucket: Optional[str] = None
    object_path: Optional[str] = None
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_uuid).first()
        if not job:
            return _error_response(404, "not_found", "Analysis job was not found.")

        files = _admin_analysis_job_files(db, job.id)
        ar_files = [
            file_record
            for file_record in files
            if _clean_text(getattr(file_record, "tool_name", None)) == ADMIN_ANALYSIS_AR_TOOL_NAME
        ]
        if len(ar_files) != 1:
            return _error_response(
                409,
                "invalid_ar_job_files",
                "Analysis job must have exactly one AR Analyzer file.",
            )

        status = (_clean_text(getattr(job, "status", None)) or "").lower()
        if status == "completed":
            return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})
        if status == "cancel_requested":
            _set_admin_analysis_job_canceled(db, job, files)
            db.commit()
            db.refresh(job)
            files = _admin_analysis_job_files(db, job.id)
            return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})

        job_file = ar_files[0]
        retrying_storage_download = False
        if status == "error":
            job_error_code = _clean_text(getattr(job, "error_code", None))
            job_file_error_code = _clean_text(getattr(job_file, "error_code", None))
            job_file_has_output = bool(
                _clean_text(getattr(job_file, "analysis_data", None))
                or getattr(job_file, "upload_id", None)
            )
            retrying_storage_download = (
                job_error_code == "storage_download_failed"
                and (
                    job_file_error_code == "storage_download_failed"
                    or not job_file_has_output
                )
            )
            if not retrying_storage_download:
                return _error_response(
                    409,
                    "invalid_job_status",
                    "Only storage download failures can be retried from error status.",
                )
        elif status not in {"queued", "processing"}:
            return _error_response(
                409,
                "invalid_job_status",
                "Analysis job must be queued or processing.",
            )

        job_file_id = job_file.id
        upload_file_id = getattr(job_file, "upload_file_id", None)
        if not upload_file_id:
            return _error_response(
                409,
                "missing_upload_file",
                "AR Analyzer file has not been persisted.",
            )

        upload_file = (
            db.query(UploadFileRecord)
            .filter(UploadFileRecord.id == upload_file_id)
            .first()
        )
        if not upload_file:
            return _error_response(
                409,
                "missing_upload_file",
                "Persisted AR file was not found.",
            )

        original_filename = (
            _clean_text(getattr(upload_file, "original_filename", None))
            or _clean_text(getattr(job_file, "original_filename", None))
            or ""
        )
        file_extension = _file_extension(original_filename)
        if file_extension not in ADMIN_ANALYSIS_AR_ALLOWED_EXTENSIONS:
            return _error_response(
                400,
                "unsupported_file_type",
                "Unsupported AR file type.",
            )

        bucket = _clean_text(getattr(upload_file, "bucket", None))
        object_path = _clean_text(getattr(upload_file, "object_path", None))
        now = datetime.now(timezone.utc)
        job.status = "processing"
        job.progress_percent = max(_optional_int(getattr(job, "progress_percent", None)) or 0, 20)
        job.current_step = (
            "Retrying AR file download"
            if retrying_storage_download
            else "Downloading AR file"
        )
        if not getattr(job, "started_at", None):
            job.started_at = now
        job.updated_at = now
        job.error_code = None
        job.error_message = None
        job.errored_at = None

        job_file.status = "processing"
        if not getattr(job_file, "started_at", None):
            job_file.started_at = now
        job_file.error_code = None
        job_file.error_message = None
        job_file.errored_at = None
        db.commit()

        logger.info("[admin_analysis] AR processing started job_id=%s", job_uuid)
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] AR processing setup failed job_id=%s", job_id)
        return _error_response(500, "analysis_processing_failed", "Unable to process AR analysis job.")
    finally:
        db.close()

    if _admin_analysis_job_cancel_requested(job_uuid):
        return _cancel_admin_analysis_job_response(job_uuid)

    try:
        file_bytes = download_upload_file_bytes(bucket or "", object_path or "")
    except AdminFinancialProcessingError as exc:
        error_message = (
            "Unable to download stored AR file."
            if exc.code == "storage_download_failed"
            else exc.message
        )
        _mark_admin_ar_processing_error(job_uuid, job_file_id, exc.code, error_message)
        return _error_response(500, exc.code, error_message)
    except Exception:
        logger.exception("[admin_analysis] AR file download failed job_id=%s", job_uuid)
        _mark_admin_ar_processing_error(
            job_uuid,
            job_file_id,
            "storage_download_failed",
            "Unable to download stored AR file.",
        )
        return _error_response(500, "storage_download_failed", "Unable to download stored AR file.")

    _update_admin_financial_processing_progress(
        job_uuid,
        job_file_id,
        35,
        "AR file downloaded",
    )
    if _admin_analysis_job_cancel_requested(job_uuid):
        return _cancel_admin_analysis_job_response(job_uuid)

    try:
        _update_admin_financial_processing_progress(
            job_uuid,
            job_file_id,
            45,
            f"Extracting {file_extension.lstrip('.').upper()} data",
        )
        if file_extension == ".csv":
            data_input = extract_csv_text(file_bytes)
            source_format = "csv"
        elif file_extension == ".xlsx":
            data_input = extract_xlsx_text(file_bytes)
            source_format = "xlsx"
        elif file_extension == ".pdf":
            data_input = extract_pdf_text(file_bytes)
            source_format = "pdf"
        else:
            raise AdminFinancialProcessingError(
                "unsupported_file_type",
                "Unsupported AR file type.",
            )
    except AdminFinancialProcessingError as exc:
        _mark_admin_ar_processing_error(job_uuid, job_file_id, exc.code, exc.message)
        return _error_response(400, exc.code, exc.message)
    except Exception:
        logger.exception(
            "[admin_analysis] AR file extraction failed job_id=%s extension=%s",
            job_uuid,
            file_extension,
        )
        if file_extension == ".csv":
            error_code = "csv_extract_failed"
            error_message = "Unable to extract AR CSV data."
        elif file_extension == ".xlsx":
            error_code = "xlsx_extract_failed"
            error_message = "Unable to extract AR XLSX data."
        elif file_extension == ".pdf":
            error_code = "pdf_extract_failed"
            error_message = "Unable to extract AR PDF text."
        else:
            error_code = "unsupported_file_type"
            error_message = "Unsupported AR file type."
        _mark_admin_ar_processing_error(
            job_uuid,
            job_file_id,
            error_code,
            error_message,
        )
        return _error_response(400, error_code, error_message)

    _update_admin_financial_processing_progress(
        job_uuid,
        job_file_id,
        60,
        "Running AR model analysis",
    )
    if _admin_analysis_job_cancel_requested(job_uuid):
        return _cancel_admin_analysis_job_response(job_uuid)

    try:
        analysis_data = run_financial_csv_analysis(
            data_input,
            cancel_checker=lambda: _admin_analysis_job_cancel_requested(job_uuid),
            source_format=source_format,
        )
    except AdminFinancialProcessingCanceled:
        return _cancel_admin_analysis_job_response(job_uuid)
    except AdminFinancialProcessingError as exc:
        _mark_admin_ar_processing_error(job_uuid, job_file_id, exc.code, exc.message)
        return _error_response(502, exc.code, exc.message)
    except Exception:
        logger.exception("[admin_analysis] AR model analysis failed job_id=%s", job_uuid)
        _mark_admin_ar_processing_error(
            job_uuid,
            job_file_id,
            "provider_unavailable",
            "The analyzer providers are temporarily unavailable. Please try again later.",
        )
        return _error_response(
            502,
            "provider_unavailable",
            "The analyzer providers are temporarily unavailable. Please try again later.",
        )

    if _admin_analysis_job_cancel_requested(job_uuid):
        return _cancel_admin_analysis_job_response(job_uuid)

    db = SessionLocal()
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_uuid).first()
        if not job:
            return _error_response(404, "not_found", "Analysis job was not found.")
        files = _admin_analysis_job_files(db, job.id)
        status = (_clean_text(getattr(job, "status", None)) or "").lower()
        if status == "cancel_requested":
            _set_admin_analysis_job_canceled(db, job, files)
            db.commit()
            db.refresh(job)
            files = _admin_analysis_job_files(db, job.id)
            return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})

        job_file = (
            db.query(AdminAnalysisJobFile)
            .filter(AdminAnalysisJobFile.id == job_file_id)
            .first()
        )
        if not job_file:
            return _error_response(404, "not_found", "Analysis job file was not found.")

        now = datetime.now(timezone.utc)
        job.status = "completed"
        job.progress_percent = 100
        job.current_step = "AR analysis processed"
        job.completed_at = now
        job.updated_at = now
        job.error_code = None
        job.error_message = None

        job_file.status = "completed"
        job_file.analysis_data = json.dumps(analysis_data)
        job_file.completed_at = now
        job_file.processed_at = now
        job_file.error_code = None
        job_file.error_message = None
        db.commit()
        db.refresh(job)
        files = _admin_analysis_job_files(db, job.id)
        logger.info("[admin_analysis] AR processing completed job_id=%s", job_uuid)
        return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] AR processing final write failed job_id=%s", job_uuid)
        _mark_admin_ar_processing_error(
            job_uuid,
            job_file_id,
            "analysis_result_store_failed",
            "Unable to store AR analysis results.",
        )
        return _error_response(
            500,
            "analysis_result_store_failed",
            "Unable to store AR analysis results.",
        )
    finally:
        db.close()


@app.post("/api/admin/analysis-jobs/{job_id}/process-claims")
def process_admin_claims_analysis_job(request: Request, job_id: str) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
    if error_response:
        return error_response

    job_uuid, validation_error = _job_uuid_or_not_found(job_id)
    if validation_error:
        return validation_error

    db = SessionLocal()
    job_file_id: Optional[object] = None
    bucket: Optional[str] = None
    object_path: Optional[str] = None
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_uuid).first()
        if not job:
            return _error_response(404, "not_found", "Analysis job was not found.")

        files = _admin_analysis_job_files(db, job.id)
        claims_files = [
            file_record
            for file_record in files
            if _clean_text(getattr(file_record, "tool_name", None)) == ADMIN_ANALYSIS_CLAIMS_TOOL_NAME
        ]
        if len(claims_files) != 1:
            return _error_response(
                409,
                "invalid_claims_job_files",
                "Analysis job must have exactly one Insurance Claim Analyzer file.",
            )

        status = (_clean_text(getattr(job, "status", None)) or "").lower()
        if status == "completed":
            return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})
        if status == "cancel_requested":
            _set_admin_analysis_job_canceled(db, job, files)
            db.commit()
            db.refresh(job)
            files = _admin_analysis_job_files(db, job.id)
            return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})

        job_file = claims_files[0]
        retrying_storage_download = False
        if status == "error":
            job_error_code = _clean_text(getattr(job, "error_code", None))
            job_file_error_code = _clean_text(getattr(job_file, "error_code", None))
            job_file_has_output = bool(
                _clean_text(getattr(job_file, "analysis_data", None))
                or getattr(job_file, "upload_id", None)
            )
            retrying_storage_download = (
                job_error_code == "storage_download_failed"
                and (
                    job_file_error_code == "storage_download_failed"
                    or not job_file_has_output
                )
            )
            if not retrying_storage_download:
                return _error_response(
                    409,
                    "invalid_job_status",
                    "Only storage download failures can be retried from error status.",
                )
        elif status not in {"queued", "processing"}:
            return _error_response(
                409,
                "invalid_job_status",
                "Analysis job must be queued or processing.",
            )

        job_file_id = job_file.id
        upload_file_id = getattr(job_file, "upload_file_id", None)
        if not upload_file_id:
            return _error_response(
                409,
                "missing_upload_file",
                "Insurance Claim Analyzer file has not been persisted.",
            )

        upload_file = (
            db.query(UploadFileRecord)
            .filter(UploadFileRecord.id == upload_file_id)
            .first()
        )
        if not upload_file:
            return _error_response(
                409,
                "missing_upload_file",
                "Persisted Claims file was not found.",
            )

        original_filename = (
            _clean_text(getattr(upload_file, "original_filename", None))
            or _clean_text(getattr(job_file, "original_filename", None))
            or ""
        )
        file_extension = _file_extension(original_filename)
        if file_extension not in ADMIN_ANALYSIS_CLAIMS_ALLOWED_EXTENSIONS:
            return _error_response(
                400,
                "unsupported_file_type",
                "Unsupported Claims file type.",
            )

        bucket = _clean_text(getattr(upload_file, "bucket", None))
        object_path = _clean_text(getattr(upload_file, "object_path", None))
        now = datetime.now(timezone.utc)
        job.status = "processing"
        job.progress_percent = max(_optional_int(getattr(job, "progress_percent", None)) or 0, 20)
        job.current_step = (
            "Retrying Claims file download"
            if retrying_storage_download
            else "Downloading Claims file"
        )
        if not getattr(job, "started_at", None):
            job.started_at = now
        job.updated_at = now
        job.error_code = None
        job.error_message = None
        job.errored_at = None

        job_file.status = "processing"
        if not getattr(job_file, "started_at", None):
            job_file.started_at = now
        job_file.error_code = None
        job_file.error_message = None
        job_file.errored_at = None
        db.commit()

        logger.info("[admin_analysis] Claims processing started job_id=%s", job_uuid)
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] Claims processing setup failed job_id=%s", job_id)
        return _error_response(500, "analysis_processing_failed", "Unable to process Claims analysis job.")
    finally:
        db.close()

    if _admin_analysis_job_cancel_requested(job_uuid):
        return _cancel_admin_analysis_job_response(job_uuid)

    try:
        file_bytes = download_upload_file_bytes(bucket or "", object_path or "")
    except AdminFinancialProcessingError as exc:
        error_message = (
            "Unable to download stored Claims file."
            if exc.code == "storage_download_failed"
            else exc.message
        )
        _mark_admin_claims_processing_error(job_uuid, job_file_id, exc.code, error_message)
        return _error_response(500, exc.code, error_message)
    except Exception:
        logger.exception("[admin_analysis] Claims file download failed job_id=%s", job_uuid)
        _mark_admin_claims_processing_error(
            job_uuid,
            job_file_id,
            "storage_download_failed",
            "Unable to download stored Claims file.",
        )
        return _error_response(500, "storage_download_failed", "Unable to download stored Claims file.")

    _update_admin_financial_processing_progress(
        job_uuid,
        job_file_id,
        35,
        "Claims file downloaded",
    )
    if _admin_analysis_job_cancel_requested(job_uuid):
        return _cancel_admin_analysis_job_response(job_uuid)

    try:
        _update_admin_financial_processing_progress(
            job_uuid,
            job_file_id,
            45,
            f"Extracting {file_extension.lstrip('.').upper()} data",
        )
        if file_extension == ".csv":
            data_input = extract_csv_text(file_bytes)
            source_format = "csv"
        elif file_extension == ".xlsx":
            data_input = extract_xlsx_text(file_bytes)
            source_format = "xlsx"
        elif file_extension == ".pdf":
            data_input = extract_pdf_text(file_bytes)
            source_format = "pdf"
        else:
            raise AdminFinancialProcessingError(
                "unsupported_file_type",
                "Unsupported Claims file type.",
            )
    except AdminFinancialProcessingError as exc:
        _mark_admin_claims_processing_error(job_uuid, job_file_id, exc.code, exc.message)
        return _error_response(400, exc.code, exc.message)
    except Exception:
        logger.exception(
            "[admin_analysis] Claims file extraction failed job_id=%s extension=%s",
            job_uuid,
            file_extension,
        )
        if file_extension == ".csv":
            error_code = "csv_extract_failed"
            error_message = "Unable to extract Claims CSV data."
        elif file_extension == ".xlsx":
            error_code = "xlsx_extract_failed"
            error_message = "Unable to extract Claims XLSX data."
        elif file_extension == ".pdf":
            error_code = "pdf_extract_failed"
            error_message = "Unable to extract Claims PDF text."
        else:
            error_code = "unsupported_file_type"
            error_message = "Unsupported Claims file type."
        _mark_admin_claims_processing_error(
            job_uuid,
            job_file_id,
            error_code,
            error_message,
        )
        return _error_response(400, error_code, error_message)

    _update_admin_financial_processing_progress(
        job_uuid,
        job_file_id,
        60,
        "Running Claims model analysis",
    )
    if _admin_analysis_job_cancel_requested(job_uuid):
        return _cancel_admin_analysis_job_response(job_uuid)

    try:
        analysis_data = run_financial_csv_analysis(
            data_input,
            cancel_checker=lambda: _admin_analysis_job_cancel_requested(job_uuid),
            source_format=source_format,
        )
    except AdminFinancialProcessingCanceled:
        return _cancel_admin_analysis_job_response(job_uuid)
    except AdminFinancialProcessingError as exc:
        _mark_admin_claims_processing_error(job_uuid, job_file_id, exc.code, exc.message)
        return _error_response(502, exc.code, exc.message)
    except Exception:
        logger.exception("[admin_analysis] Claims model analysis failed job_id=%s", job_uuid)
        _mark_admin_claims_processing_error(
            job_uuid,
            job_file_id,
            "provider_unavailable",
            "The analyzer providers are temporarily unavailable. Please try again later.",
        )
        return _error_response(
            502,
            "provider_unavailable",
            "The analyzer providers are temporarily unavailable. Please try again later.",
        )

    if _admin_analysis_job_cancel_requested(job_uuid):
        return _cancel_admin_analysis_job_response(job_uuid)

    db = SessionLocal()
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_uuid).first()
        if not job:
            return _error_response(404, "not_found", "Analysis job was not found.")
        files = _admin_analysis_job_files(db, job.id)
        status = (_clean_text(getattr(job, "status", None)) or "").lower()
        if status == "cancel_requested":
            _set_admin_analysis_job_canceled(db, job, files)
            db.commit()
            db.refresh(job)
            files = _admin_analysis_job_files(db, job.id)
            return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})

        job_file = (
            db.query(AdminAnalysisJobFile)
            .filter(AdminAnalysisJobFile.id == job_file_id)
            .first()
        )
        if not job_file:
            return _error_response(404, "not_found", "Analysis job file was not found.")

        now = datetime.now(timezone.utc)
        job.status = "completed"
        job.progress_percent = 100
        job.current_step = "Claims analysis processed"
        job.completed_at = now
        job.updated_at = now
        job.error_code = None
        job.error_message = None

        job_file.status = "completed"
        job_file.analysis_data = json.dumps(analysis_data)
        job_file.completed_at = now
        job_file.processed_at = now
        job_file.error_code = None
        job_file.error_message = None
        db.commit()
        db.refresh(job)
        files = _admin_analysis_job_files(db, job.id)
        logger.info("[admin_analysis] Claims processing completed job_id=%s", job_uuid)
        return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] Claims processing final write failed job_id=%s", job_uuid)
        _mark_admin_claims_processing_error(
            job_uuid,
            job_file_id,
            "analysis_result_store_failed",
            "Unable to store Claims analysis results.",
        )
        return _error_response(
            500,
            "analysis_result_store_failed",
            "Unable to store Claims analysis results.",
        )
    finally:
        db.close()


@app.post("/api/admin/analysis-jobs/{job_id}/promote-financial")
def promote_admin_financial_analysis_job(request: Request, job_id: str) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
    if error_response:
        return error_response

    job_uuid, validation_error = _job_uuid_or_not_found(job_id)
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_uuid).first()
        if not job:
            return _error_response(404, "not_found", "Analysis job was not found.")

        if (_clean_text(getattr(job, "status", None)) or "").lower() != "completed":
            return _error_response(
                409,
                "invalid_job_status",
                "Financial analysis job must be completed before promotion.",
            )

        files = _admin_analysis_job_files(db, job.id)
        financial_files = [
            file_record
            for file_record in files
            if _clean_text(getattr(file_record, "tool_name", None)) == ADMIN_ANALYSIS_FINANCIAL_TOOL_NAME
        ]
        if len(financial_files) != 1:
            return _error_response(
                409,
                "invalid_financial_job_files",
                "Analysis job must have exactly one Financial Analyzer file.",
            )

        job_file = financial_files[0]
        if (_clean_text(getattr(job_file, "status", None)) or "").lower() != "completed":
            return _error_response(
                409,
                "invalid_job_file_status",
                "Financial Analyzer file must be completed before promotion.",
            )

        analysis_data, analysis_error = _admin_financial_analysis_data_object(
            getattr(job_file, "analysis_data", None)
        )
        if analysis_error:
            return analysis_error

        upload_file_id = getattr(job_file, "upload_file_id", None)
        if not upload_file_id:
            return _error_response(
                409,
                "missing_upload_file",
                "Financial Analyzer file has not been persisted.",
            )

        upload_file = (
            db.query(UploadFileRecord)
            .filter(UploadFileRecord.id == upload_file_id)
            .first()
        )
        if not upload_file:
            return _error_response(
                409,
                "missing_upload_file",
                "Persisted financial file was not found.",
            )

        client_email, validation_error = _required_email(getattr(job, "client_email", None))
        if validation_error:
            return validation_error

        promotion_result, promotion_error = _promote_financial_admin_job_records(
            db=db,
            job=job,
            job_file=job_file,
            upload_file=upload_file,
            analysis_data=analysis_data,
            client_email=client_email,
        )
        if promotion_error:
            db.rollback()
            return promotion_error

        db.commit()
        db.refresh(job)
        files = _admin_analysis_job_files(db, job.id)
        return JSONResponse(
            {
                "ok": True,
                "job": _admin_analysis_job_payload(job, files),
                "submissionId": promotion_result["submission_id"],
                "uploadId": promotion_result["upload_id"],
                "promoted": promotion_result["promoted"],
            }
        )
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] financial promotion failed job_id=%s", job_id)
        return _error_response(
            500,
            "analysis_promotion_failed",
            "Unable to promote financial analysis job.",
        )
    finally:
        db.close()


@app.post("/api/admin/analysis-jobs/{job_id}/promote-ar")
def promote_admin_ar_analysis_job(request: Request, job_id: str) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
    if error_response:
        return error_response

    job_uuid, validation_error = _job_uuid_or_not_found(job_id)
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_uuid).first()
        if not job:
            return _error_response(404, "not_found", "Analysis job was not found.")

        if (_clean_text(getattr(job, "status", None)) or "").lower() != "completed":
            return _error_response(
                409,
                "invalid_job_status",
                "AR analysis job must be completed before promotion.",
            )

        files = _admin_analysis_job_files(db, job.id)
        ar_files = [
            file_record
            for file_record in files
            if _clean_text(getattr(file_record, "tool_name", None)) == ADMIN_ANALYSIS_AR_TOOL_NAME
        ]
        if len(ar_files) != 1:
            return _error_response(
                409,
                "invalid_ar_job_files",
                "Analysis job must have exactly one AR Analyzer file.",
            )

        job_file = ar_files[0]
        if (_clean_text(getattr(job_file, "status", None)) or "").lower() != "completed":
            return _error_response(
                409,
                "invalid_job_file_status",
                "AR Analyzer file must be completed before promotion.",
            )

        analysis_data, analysis_error = _admin_ar_analysis_data_object(
            getattr(job_file, "analysis_data", None)
        )
        if analysis_error:
            return analysis_error

        upload_file_id = getattr(job_file, "upload_file_id", None)
        if not upload_file_id:
            return _error_response(
                409,
                "missing_upload_file",
                "AR Analyzer file has not been persisted.",
            )

        upload_file = (
            db.query(UploadFileRecord)
            .filter(UploadFileRecord.id == upload_file_id)
            .first()
        )
        if not upload_file:
            return _error_response(
                409,
                "missing_upload_file",
                "Persisted AR file was not found.",
            )

        client_email, validation_error = _required_email(getattr(job, "client_email", None))
        if validation_error:
            return validation_error

        promotion_result, promotion_error = _promote_ar_admin_job_records(
            db=db,
            job=job,
            job_file=job_file,
            upload_file=upload_file,
            analysis_data=analysis_data,
            client_email=client_email,
        )
        if promotion_error:
            db.rollback()
            return promotion_error

        db.commit()
        db.refresh(job)
        files = _admin_analysis_job_files(db, job.id)
        return JSONResponse(
            {
                "ok": True,
                "job": _admin_analysis_job_payload(job, files),
                "submissionId": promotion_result["submission_id"],
                "uploadId": promotion_result["upload_id"],
                "promoted": promotion_result["promoted"],
            }
        )
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] AR promotion failed job_id=%s", job_id)
        return _error_response(
            500,
            "analysis_promotion_failed",
            "Unable to promote AR analysis job.",
        )
    finally:
        db.close()


@app.post("/api/admin/analysis-jobs/{job_id}/promote-claims")
def promote_admin_claims_analysis_job(request: Request, job_id: str) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
    if error_response:
        return error_response

    job_uuid, validation_error = _job_uuid_or_not_found(job_id)
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_uuid).first()
        if not job:
            return _error_response(404, "not_found", "Analysis job was not found.")

        if (_clean_text(getattr(job, "status", None)) or "").lower() != "completed":
            return _error_response(
                409,
                "invalid_job_status",
                "Claims analysis job must be completed before promotion.",
            )

        files = _admin_analysis_job_files(db, job.id)
        claims_files = [
            file_record
            for file_record in files
            if _clean_text(getattr(file_record, "tool_name", None)) == ADMIN_ANALYSIS_CLAIMS_TOOL_NAME
        ]
        if len(claims_files) != 1:
            return _error_response(
                409,
                "invalid_claims_job_files",
                "Analysis job must have exactly one Insurance Claim Analyzer file.",
            )

        job_file = claims_files[0]
        if (_clean_text(getattr(job_file, "status", None)) or "").lower() != "completed":
            return _error_response(
                409,
                "invalid_job_file_status",
                "Insurance Claim Analyzer file must be completed before promotion.",
            )

        analysis_data, analysis_error = _admin_claims_analysis_data_object(
            getattr(job_file, "analysis_data", None)
        )
        if analysis_error:
            return analysis_error

        upload_file_id = getattr(job_file, "upload_file_id", None)
        if not upload_file_id:
            return _error_response(
                409,
                "missing_upload_file",
                "Insurance Claim Analyzer file has not been persisted.",
            )

        upload_file = (
            db.query(UploadFileRecord)
            .filter(UploadFileRecord.id == upload_file_id)
            .first()
        )
        if not upload_file:
            return _error_response(
                409,
                "missing_upload_file",
                "Persisted Claims file was not found.",
            )

        client_email, validation_error = _required_email(getattr(job, "client_email", None))
        if validation_error:
            return validation_error

        promotion_result, promotion_error = _promote_claims_admin_job_records(
            db=db,
            job=job,
            job_file=job_file,
            upload_file=upload_file,
            analysis_data=analysis_data,
            client_email=client_email,
        )
        if promotion_error:
            db.rollback()
            return promotion_error

        db.commit()
        db.refresh(job)
        files = _admin_analysis_job_files(db, job.id)
        return JSONResponse(
            {
                "ok": True,
                "job": _admin_analysis_job_payload(job, files),
                "submissionId": promotion_result["submission_id"],
                "uploadId": promotion_result["upload_id"],
                "promoted": promotion_result["promoted"],
            }
        )
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] Claims promotion failed job_id=%s", job_id)
        return _error_response(
            500,
            "analysis_promotion_failed",
            "Unable to promote Claims analysis job.",
        )
    finally:
        db.close()


@app.get("/api/admin/pdf-generator/options")
def get_admin_pdf_generator_options(request: Request) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_PDF_READ)
    if error_response:
        return error_response

    db = SessionLocal()
    try:
        submissions = (
            db.query(ClientSubmission)
            .filter(ClientSubmission.user_email.isnot(None))
            .order_by(ClientSubmission.submitted_at.desc())
            .all()
        )
        candidate_emails: dict[str, dict[str, Any]] = {}
        for submission in submissions:
            email = (_clean_text(getattr(submission, "user_email", None)) or "").lower()
            if not email:
                continue
            candidate = candidate_emails.setdefault(
                email,
                {
                    "email": email,
                    "submission_count": 0,
                    "eligible_upload_count": 0,
                    "latest_submitted_at": None,
                    "latest_upload_time": None,
                },
            )
            candidate["submission_count"] += 1
            submitted_at = getattr(submission, "submitted_at", None)
            if _datetime_timestamp(submitted_at) > _datetime_timestamp(candidate["latest_submitted_at"]):
                candidate["latest_submitted_at"] = submitted_at

        if candidate_emails:
            uploads = (
                db.query(Upload)
                .filter(func.lower(Upload.user_email).in_(list(candidate_emails.keys())))
                .filter(Upload.analysis_data.isnot(None))
                .all()
            )
            for upload in uploads:
                if not _clean_text(getattr(upload, "analysis_data", None)):
                    continue
                email = (_clean_text(getattr(upload, "user_email", None)) or "").lower()
                candidate = candidate_emails.get(email)
                if not candidate:
                    continue
                candidate["eligible_upload_count"] += 1
                upload_time = getattr(upload, "upload_time", None)
                if _datetime_timestamp(_coerce_datetime(upload_time)) > _datetime_timestamp(
                    _coerce_datetime(candidate["latest_upload_time"])
                ):
                    candidate["latest_upload_time"] = upload_time

        clients = [
            {
                "email": item["email"],
                "submissionCount": item["submission_count"],
                "eligibleUploadCount": item["eligible_upload_count"],
                "latestSubmittedAt": _iso_datetime(item["latest_submitted_at"]),
                "latestUploadTime": _clean_text(item["latest_upload_time"]),
            }
            for item in candidate_emails.values()
            if item["eligible_upload_count"] > 0
        ]
        clients.sort(key=lambda item: item["email"])

        return JSONResponse(
            {
                "ok": True,
                "clients": clients,
                "count": len(clients),
            }
        )
    except Exception:
        logger.exception("[admin_pdf_generator] options lookup failed")
        return _error_response(500, "pdf_generator_options_failed", "Unable to load PDF generator options.")
    finally:
        db.close()


@app.get("/api/admin/pdf-generator/client")
def get_admin_pdf_generator_client(
    request: Request,
    email: Optional[str] = None,
) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_PDF_READ)
    if error_response:
        return error_response

    client_email, validation_error = _required_email(email)
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        submissions = (
            db.query(ClientSubmission)
            .filter(func.lower(ClientSubmission.user_email) == client_email)
            .order_by(ClientSubmission.submitted_at.desc())
            .all()
        )
        uploads = (
            db.query(Upload)
            .filter(func.lower(Upload.user_email) == client_email)
            .filter(Upload.analysis_data.isnot(None))
            .order_by(Upload.upload_time.desc())
            .all()
        )
        eligible_uploads = [
            upload for upload in uploads if _clean_text(getattr(upload, "analysis_data", None))
        ]

        return JSONResponse(
            {
                "ok": True,
                "clientEmail": client_email,
                "submissions": [
                    _pdf_generator_submission_payload(submission)
                    for submission in submissions
                ],
                "uploads": [
                    _pdf_generator_upload_payload(upload)
                    for upload in eligible_uploads
                ],
                "count": len(eligible_uploads),
            }
        )
    except Exception:
        logger.exception("[admin_pdf_generator] client lookup failed client_email=%s", client_email)
        return _error_response(500, "pdf_generator_client_failed", "Unable to load PDF generator client details.")
    finally:
        db.close()


@app.post("/api/admin/pdf-generator/generate")
async def generate_admin_pdf_report(request: Request) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_PDF_GENERATE)
    if error_response:
        return error_response

    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    (
        upload_id,
        opportunities,
        trends,
        key_trends,
        additional_notes,
        validation_error,
    ) = _validate_pdf_generation_body(body)
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        upload = db.query(Upload).filter(Upload.id == upload_id).first()
        if not upload:
            return _error_response(404, "upload_not_found", "Upload was not found.")

        analysis_payload = _pdf_generator_analysis_payload(getattr(upload, "analysis_data", None))
        if analysis_payload is None:
            return _error_response(
                400,
                "analysis_data_unavailable",
                "Upload analysis data is missing or unreadable.",
            )

        client_email = _clean_text(getattr(upload, "user_email", None)) or ""
        submission = None
        submission_id = getattr(upload, "submission_id", None)
        if submission_id:
            submission = db.query(ClientSubmission).filter(ClientSubmission.id == submission_id).first()
        if not submission and client_email:
            submission = (
                db.query(ClientSubmission)
                .filter(func.lower(ClientSubmission.user_email) == client_email.lower())
                .order_by(ClientSubmission.submitted_at.desc())
                .first()
            )

        current_version = _optional_int(getattr(upload, "pdf_version", None)) or 0
        next_version = current_version + 1
        upload_id_text = str(getattr(upload, "id"))
        tool_name = _clean_text(getattr(upload, "tool_name", None)) or "analysis"
        upload_time = _clean_text(getattr(upload, "upload_time", None)) or "-"
        metadata = {
            "client_name": _full_name(submission) or client_email or "Unknown client",
            "office_name": _clean_text(getattr(submission, "office_name", None)) if submission else "",
            "client_email": client_email,
            "tool_name": tool_name,
            "upload_time": upload_time,
        }
    except Exception:
        logger.exception("[admin_pdf_generator] generate lookup failed upload_id=%s", upload_id)
        return _error_response(500, "pdf_generation_lookup_failed", "Unable to load upload for PDF generation.")
    finally:
        db.close()

    date_prefix = datetime.utcnow().strftime("%Y-%m-%d")
    safe_email = safe_path_component(client_email)
    safe_tool = safe_path_component(tool_name)
    file_name = f"{safe_email}_{safe_tool}_{date_prefix}_v{next_version}.pdf"
    object_path = f"reports/{safe_email}/{date_prefix}/{safe_tool}/{upload_id_text}/{file_name}"
    sections = {
        "opportunities": opportunities,
        "trends": trends,
        "key_trends": key_trends,
    }

    try:
        pdf_bytes = generate_pdf_bytes(metadata, sections, additional_notes, next_version)
    except Exception:
        logger.exception("[admin_pdf_generator] pdf generation failed upload_id=%s", upload_id)
        return _error_response(500, "pdf_generation_failed", "Unable to generate PDF.")

    pdf_url, upload_error = upload_pdf_report(pdf_bytes, object_path)
    if upload_error:
        logger.error(
            "[admin_pdf_generator] pdf upload failed upload_id=%s path=%s error_type=%s",
            upload_id,
            object_path,
            type(upload_error).__name__,
        )
        return _error_response(500, "pdf_upload_failed", "Unable to save PDF to storage.")

    signed_url, signed_warning = create_report_signed_url(object_path)
    warnings = [signed_warning] if signed_warning else []
    generated_at = datetime.now(timezone.utc)

    db = SessionLocal()
    metadata_committed = False
    try:
        upload = db.query(Upload).filter(Upload.id == upload_id).first()
        if not upload:
            cleanup_pdf_report(object_path)
            return _error_response(404, "upload_not_found", "Upload was not found.")

        upload.pdf_version = next_version
        upload.pdf_url = pdf_url
        upload.pdf_generated_at = generated_at
        db.commit()
        metadata_committed = True
        db.refresh(upload)

        upload_payload = _pdf_generator_upload_payload(upload)
        payload_pdf = upload_payload.get("pdf")
        if isinstance(payload_pdf, dict):
            payload_pdf["reportPath"] = object_path
            payload_pdf["signedUrl"] = signed_url
        payload_warnings = upload_payload.get("warnings")
        if isinstance(payload_warnings, list):
            for warning in payload_warnings:
                if warning and warning not in warnings:
                    warnings.append(warning)

        logger.info("[admin_pdf_generator] generated upload_id=%s version=%s", upload_id, next_version)
        return JSONResponse(
            {
                "ok": True,
                "upload": upload_payload,
                "pdf": {
                    "pdfVersion": next_version,
                    "pdfUrl": pdf_url,
                    "pdfGeneratedAt": _iso_datetime(generated_at),
                    "reportPath": object_path,
                    "signedUrl": signed_url,
                },
                "warnings": warnings,
            }
        )
    except Exception:
        db.rollback()
        if not metadata_committed:
            cleanup_pdf_report(object_path)
        logger.exception("[admin_pdf_generator] pdf metadata update failed upload_id=%s", upload_id)
        return _error_response(
            500,
            "pdf_metadata_update_failed",
            "PDF was generated but metadata could not be updated.",
        )
    finally:
        db.close()


@app.get("/api/admin/secure-uploads/files")
def list_admin_secure_upload_files(
    request: Request,
    completedOnly: bool = True,
    email: Optional[str] = None,
    startDate: Optional[str] = None,
    endDate: Optional[str] = None,
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_SECURE_UPLOADS_READ)
    if error_response:
        return error_response

    start_dt, start_error = _admin_date_filter_start(startDate, "startDate")
    if start_error:
        return start_error
    end_dt, end_error = _admin_date_filter_start(endDate, "endDate")
    if end_error:
        return end_error
    if start_dt and end_dt and end_dt < start_dt:
        return _error_response(400, "invalid_date_range", "endDate must be on or after startDate.")

    safe_limit = min(limit, 100)
    normalized_email = (_clean_text(email) or "").lower()
    end_exclusive = end_dt + timedelta(days=1) if end_dt else None

    db = SessionLocal()
    try:
        query = db.query(UploadPortalFile)
        if completedOnly:
            query = query.filter(UploadPortalFile.completed_at.isnot(None))
        if normalized_email:
            query = query.filter(UploadPortalFile.user_email.ilike(f"%{normalized_email}%"))
        if start_dt:
            query = query.filter(UploadPortalFile.created_at >= start_dt)
        if end_exclusive:
            query = query.filter(UploadPortalFile.created_at < end_exclusive)

        rows = (
            query.order_by(UploadPortalFile.created_at.desc())
            .offset(offset)
            .limit(safe_limit + 1)
            .all()
        )
        has_more = len(rows) > safe_limit
        rows = rows[:safe_limit]

        return JSONResponse(
            {
                "ok": True,
                "items": [_secure_upload_file_payload(row) for row in rows],
                "count": len(rows),
                "limit": safe_limit,
                "offset": offset,
                "hasMore": has_more,
            }
        )
    except Exception:
        logger.exception("[admin_secure_uploads] file inbox lookup failed")
        return _error_response(500, "secure_uploads_lookup_failed", "Unable to load secure upload files.")
    finally:
        db.close()


@app.post("/api/admin/secure-uploads/requests")
async def create_admin_secure_upload_request(request: Request) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_SECURE_UPLOADS_WRITE)
    if error_response:
        return error_response

    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    email_value = body.get("clientEmail")
    if email_value is None:
        email_value = body.get("email")

    client_email, validation_error = _required_email(email_value)
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        user_exists = (
            db.query(User.id)
            .filter(func.lower(User.email) == client_email)
            .first()
            is not None
        )
    except Exception:
        logger.exception(
            "[admin_secure_uploads] user lookup failed client_email=%s",
            client_email,
        )
        return _error_response(
            500,
            "secure_upload_user_lookup_failed",
            "Unable to verify secure upload client user.",
        )
    finally:
        db.close()

    if not user_exists:
        return _error_response(
            404,
            "secure_upload_user_not_found",
            "Secure upload requests can only be sent to an existing client user.",
        )

    try:
        result = create_upload_request(client_email, request_ip=_request_client_ip(request))
    except PortalError as exc:
        status_code = exc.status if isinstance(exc.status, int) else 400
        safe_message = exc.message or "Unable to create secure upload request."
        logger.warning(
            "[admin_secure_uploads] request creation rejected client_email=%s code=%s",
            client_email,
            exc.code,
        )
        return _error_response(status_code, exc.code, safe_message)
    except Exception:
        logger.exception(
            "[admin_secure_uploads] request creation failed client_email=%s",
            client_email,
        )
        return _error_response(
            500,
            "secure_upload_request_failed",
            "Unable to create secure upload request.",
        )

    return JSONResponse(
        {
            "ok": True,
            "request": {
                "requestId": _clean_text(result.get("request_id")),
                "clientEmail": client_email,
                "expiresAt": _clean_text(result.get("expires_at")),
                "expiresInMinutes": _portal_token_ttl_minutes(),
                "emailSent": True,
            },
        }
    )


@app.post("/api/admin/billing/checkout-sessions")
async def create_admin_checkout_session(request: Request) -> JSONResponse:
    admin_user, _, error_response = _require_admin_permission(request, PERMISSION_BILLING_WRITE)
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
    upload_ids, validation_error = _checkout_upload_ids_from_body(body)
    if validation_error:
        return validation_error
    upload_id = upload_ids[0] if upload_ids else None
    client_submission_id, validation_error = _optional_uuid(
        body.get("clientSubmissionId"),
        "clientSubmissionId",
    )
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        upload_records, validation_error = _validate_checkout_uploads(db, upload_ids, client_email)
        if validation_error:
            return validation_error
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
            "upload_count": str(len(upload_ids)),
        }
        if upload_id:
            metadata["upload_id"] = str(upload_id)
        upload_ids_metadata = ",".join(str(selected_upload_id) for selected_upload_id in upload_ids)
        if upload_ids_metadata and len(upload_ids_metadata) <= 500:
            metadata["upload_ids"] = upload_ids_metadata
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
        db.flush()
        for selected_upload_id in upload_ids:
            db.add(
                StripeCheckoutSessionUpload(
                    checkout_session_id=local_session.id,
                    upload_id=selected_upload_id,
                )
            )
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
                "uploadId": _id_text(upload_id),
                "clientSubmissionId": _id_text(client_submission_id),
                "uploadIds": [_id_text(selected_upload_id) for selected_upload_id in upload_ids],
                "relatedUploads": [_upload_payload(upload) for upload in upload_records],
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
    _, _, error_response = _require_admin_permission(request, PERMISSION_BILLING_READ)
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
        checkout_related_uploads = _checkout_related_uploads_by_session(db, checkout_sessions)

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
                    _checkout_session_payload(
                        latest_paid_session,
                        checkout_related_uploads.get(_id_text(getattr(latest_paid_session, "id", None)) or "", []),
                    ) if latest_paid_session else None
                ),
                "checkoutSessions": [
                    _checkout_session_payload(
                        session,
                        checkout_related_uploads.get(_id_text(getattr(session, "id", None)) or "", []),
                    )
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
    _, _, error_response = _require_admin_permission(request, PERMISSION_BILLING_READ)
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
        checkout_related_uploads = _checkout_related_uploads_by_session(db, checkout_rows)

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
                    _checkout_session_payload(
                        session,
                        checkout_related_uploads.get(_id_text(getattr(session, "id", None)) or "", []),
                    )
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
    admin_user, _, error_response = _require_admin_permission(request, PERMISSION_BILLING_WRITE)
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
    if _checkout_session_is_paid_or_complete(local_session):
        _mark_checkout_session_uploads_paid(db, local_session)
    logger.info(
        "[admin_api] Stripe checkout session completed session_id=%s status=%s payment_status=%s",
        checkout_session_id,
        local_session.status,
        local_session.payment_status,
    )
    return "processed"


def _checkout_session_is_paid_or_complete(session: StripeCheckoutSession) -> bool:
    payment_status = (_clean_text(getattr(session, "payment_status", None)) or "").lower()
    status = (_clean_text(getattr(session, "status", None)) or "").lower()
    return payment_status == "paid" or status in {"complete", "completed"}


def _mark_checkout_session_uploads_paid(db: Any, session: StripeCheckoutSession) -> int:
    session_db_id = getattr(session, "id", None)
    stripe_session_id = _clean_text(getattr(session, "stripe_checkout_session_id", None))
    upload_ids = []
    seen_upload_ids = set()

    if session_db_id:
        link_rows = (
            db.query(StripeCheckoutSessionUpload.upload_id)
            .filter(StripeCheckoutSessionUpload.checkout_session_id == session_db_id)
            .all()
        )
        for row in link_rows:
            upload_id = row[0]
            upload_id_text = _id_text(upload_id)
            if upload_id and upload_id_text and upload_id_text not in seen_upload_ids:
                upload_ids.append(upload_id)
                seen_upload_ids.add(upload_id_text)

    legacy_upload_id = getattr(session, "upload_id", None)
    legacy_upload_id_text = _id_text(legacy_upload_id)
    if legacy_upload_id and legacy_upload_id_text and legacy_upload_id_text not in seen_upload_ids:
        upload_ids.append(legacy_upload_id)

    if not upload_ids:
        logger.info(
            "[admin_api] checkout_session_uploads_paid session_id=%s local_session_id=%s marked_upload_count=0",
            stripe_session_id,
            _id_text(session_db_id),
        )
        return 0

    marked_count = (
        db.query(Upload)
        .filter(Upload.id.in_(upload_ids))
        .filter(Upload.paid.is_(False))
        .update({Upload.paid: True}, synchronize_session=False)
    )
    logger.info(
        "[admin_api] checkout_session_uploads_paid session_id=%s local_session_id=%s related_upload_count=%s marked_upload_count=%s",
        stripe_session_id,
        _id_text(session_db_id),
        len(upload_ids),
        marked_count,
    )
    return int(marked_count or 0)


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


def _ensure_client_option(
    candidates: dict[str, dict[str, Any]],
    email_value: object,
) -> Optional[dict[str, Any]]:
    email = (_clean_text(email_value) or "").lower()
    if not email:
        return None
    if email not in candidates:
        candidates[email] = {
            "email": email,
            "firstName": None,
            "lastName": None,
            "officeName": None,
            "orgType": None,
            "phone": None,
            "ghlCid": None,
            "_latestSubmittedAt": None,
        }
    return candidates[email]


def _merge_client_option_value(candidate: dict[str, Any], key: str, value: object) -> None:
    cleaned = _clean_text(value)
    if cleaned and not candidate.get(key):
        candidate[key] = cleaned


def _merge_client_option_submission(
    candidates: dict[str, dict[str, Any]],
    submission: ClientSubmission,
) -> None:
    candidate = _ensure_client_option(candidates, getattr(submission, "user_email", None))
    if candidate is None:
        return

    submitted_at = getattr(submission, "submitted_at", None)
    current_latest = candidate.get("_latestSubmittedAt")
    is_new_latest = (
        isinstance(submitted_at, datetime)
        and (
            not isinstance(current_latest, datetime)
            or submitted_at > current_latest
        )
    )
    if is_new_latest:
        candidate["_latestSubmittedAt"] = submitted_at

    field_map = {
        "firstName": "first_name",
        "lastName": "last_name",
        "officeName": "office_name",
        "orgType": "org_type",
        "phone": "phone",
        "ghlCid": "ghl_cid",
    }
    for payload_key, model_key in field_map.items():
        cleaned = _clean_text(getattr(submission, model_key, None))
        if cleaned and (is_new_latest or not candidate.get(payload_key)):
            candidate[payload_key] = cleaned


def _merge_client_option_user(
    candidates: dict[str, dict[str, Any]],
    user: User,
) -> None:
    candidate = _ensure_client_option(candidates, getattr(user, "email", None))
    if candidate is None:
        return

    field_map = {
        "firstName": "first_name",
        "lastName": "last_name",
        "officeName": "office_name",
        "orgType": "org_type",
        "phone": "phone",
    }
    for payload_key, model_key in field_map.items():
        _merge_client_option_value(candidate, payload_key, getattr(user, model_key, None))


def _datetime_timestamp(value: object) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return 0.0


def _coerce_datetime(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    text = _clean_text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def _client_option_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": _clean_text(candidate.get("email")),
        "firstName": _clean_text(candidate.get("firstName")),
        "lastName": _clean_text(candidate.get("lastName")),
        "officeName": _clean_text(candidate.get("officeName")),
        "orgType": _clean_text(candidate.get("orgType")),
        "phone": _clean_text(candidate.get("phone")),
        "ghlCid": _clean_text(candidate.get("ghlCid")),
        "latestSubmittedAt": _iso_datetime(candidate.get("_latestSubmittedAt")),
    }


def _validate_admin_analysis_file_inputs(
    value: object,
) -> tuple[list[dict[str, Any]], Optional[JSONResponse]]:
    if not isinstance(value, list) or not value:
        return [], _error_response(400, "missing_files", "At least one file metadata item is required.")

    validated_files: list[dict[str, Any]] = []
    for index, file_item in enumerate(value):
        if not isinstance(file_item, dict):
            return [], _error_response(400, "invalid_files", "Each file item must be an object.")
        for disallowed_key in ("file", "fileBytes", "content", "data", "base64"):
            if file_item.get(disallowed_key) is not None:
                return [], _error_response(
                    400,
                    "file_contents_not_accepted",
                    "This endpoint accepts file metadata only.",
                )

        tool_name = _clean_text(file_item.get("toolName"))
        if not tool_name:
            return [], _error_response(400, "missing_toolName", "toolName is required.")
        if tool_name not in ADMIN_ANALYSIS_ALLOWED_TOOL_NAMES:
            return [], _error_response(400, "invalid_toolName", "Unsupported analysis tool.")

        byte_size = file_item.get("byteSize")
        if byte_size is not None:
            if isinstance(byte_size, bool) or not isinstance(byte_size, int) or byte_size < 0:
                return [], _error_response(400, "invalid_byteSize", "byteSize must be a non-negative integer.")

        validated_files.append(
            {
                "tool_name": tool_name,
                "original_filename": _clean_text(file_item.get("originalFilename")),
                "content_type": _clean_text(file_item.get("contentType")),
                "byte_size": byte_size,
                "index": index,
            }
        )

    return validated_files, None


async def _validate_single_file_field(
    request: Request,
    field_name: str,
) -> Optional[JSONResponse]:
    try:
        # Starlette caches parsed multipart form data; this checks field count
        # without reading the uploaded file stream.
        form = await request.form()
    except Exception:
        logger.exception("[admin_api] invalid multipart form for admin analysis intake.")
        return _error_response(400, "invalid_multipart", "Request must be valid multipart form data.")

    values = form.getlist(field_name)
    if len(values) > 1:
        return _error_response(400, "too_many_files", "Only one financialFile upload is allowed.")
    return None


def _file_extension(filename: object) -> str:
    filename_text = _clean_text(filename) or ""
    return os.path.splitext(filename_text)[1].lower()


def _admin_analysis_max_file_bytes() -> int:
    raw_value = os.getenv(
        "ADMIN_ANALYSIS_MAX_FILE_MB",
        str(DEFAULT_ADMIN_ANALYSIS_MAX_FILE_MB),
    ).strip()
    try:
        max_file_mb = int(raw_value)
    except ValueError:
        max_file_mb = DEFAULT_ADMIN_ANALYSIS_MAX_FILE_MB
    return max(1, max_file_mb) * 1024 * 1024


async def _read_admin_upload_file(
    upload_file: FastAPIUploadFile,
) -> tuple[bytes, Optional[JSONResponse]]:
    max_file_bytes = _admin_analysis_max_file_bytes()
    chunks: list[bytes] = []
    total_bytes = 0

    while True:
        chunk = await upload_file.read(ADMIN_ANALYSIS_READ_CHUNK_SIZE)
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > max_file_bytes:
            return b"", _error_response(400, "file_too_large", "Uploaded file exceeds the size limit.")
        chunks.append(chunk)

    if total_bytes == 0:
        return b"", _error_response(400, "empty_file", "Uploaded file is empty.")
    return b"".join(chunks), None


def _mark_admin_financial_intake_failed(
    job_id: object,
    job_file_id: object,
    error_code: str,
    error_message: str,
) -> None:
    if not job_id:
        return

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_id).first()
        if job:
            job.status = "error"
            job.current_step = "Financial file intake failed"
            job.error_code = error_code
            job.error_message = error_message
            job.errored_at = now
            job.updated_at = now

        if job_file_id:
            job_file = (
                db.query(AdminAnalysisJobFile)
                .filter(AdminAnalysisJobFile.id == job_file_id)
                .first()
            )
            if job_file:
                job_file.status = "error"
                job_file.error_code = error_code
                job_file.error_message = error_message
                job_file.errored_at = now

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[admin_api] failed to mark financial intake failed job_id=%s", job_id)
    finally:
        db.close()


def _mark_admin_ar_intake_failed(
    job_id: object,
    job_file_id: object,
    error_code: str,
    error_message: str,
) -> None:
    if not job_id:
        return

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_id).first()
        if job:
            job.status = "error"
            job.current_step = "AR file intake failed"
            job.error_code = error_code
            job.error_message = error_message
            job.errored_at = now
            job.updated_at = now

        if job_file_id:
            job_file = (
                db.query(AdminAnalysisJobFile)
                .filter(AdminAnalysisJobFile.id == job_file_id)
                .first()
            )
            if job_file:
                job_file.status = "error"
                job_file.error_code = error_code
                job_file.error_message = error_message
                job_file.errored_at = now

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[admin_api] failed to mark AR intake failed job_id=%s", job_id)
    finally:
        db.close()


def _mark_admin_claims_intake_failed(
    job_id: object,
    job_file_id: object,
    error_code: str,
    error_message: str,
) -> None:
    if not job_id:
        return

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_id).first()
        if job:
            job.status = "error"
            job.current_step = "Claims file intake failed"
            job.error_code = error_code
            job.error_message = error_message
            job.errored_at = now
            job.updated_at = now

        if job_file_id:
            job_file = (
                db.query(AdminAnalysisJobFile)
                .filter(AdminAnalysisJobFile.id == job_file_id)
                .first()
            )
            if job_file:
                job_file.status = "error"
                job_file.error_code = error_code
                job_file.error_message = error_message
                job_file.errored_at = now

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[admin_api] failed to mark Claims intake failed job_id=%s", job_id)
    finally:
        db.close()


def _cleanup_admin_intake_upload_file(upload_file_id: object) -> None:
    if not upload_file_id:
        return

    db = SessionLocal()
    try:
        upload_file = (
            db.query(UploadFileRecord)
            .filter(UploadFileRecord.id == upload_file_id)
            .first()
        )
        if not upload_file:
            return

        bucket = _clean_text(getattr(upload_file, "bucket", None))
        object_path = _clean_text(getattr(upload_file, "object_path", None))
        if bucket and object_path:
            try:
                client = _get_supabase_admin_client()
                if client:
                    client.storage.from_(bucket).remove([object_path])
            except Exception:
                logger.exception(
                    "[admin_api] best-effort intake storage cleanup failed upload_file_id=%s",
                    upload_file_id,
                )

        db.delete(upload_file)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "[admin_api] best-effort UploadFile cleanup failed upload_file_id=%s",
            upload_file_id,
        )
    finally:
        db.close()


def _admin_analysis_job_cancel_requested(job_id: object) -> bool:
    if not job_id:
        return False

    db = SessionLocal()
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_id).first()
        return (_clean_text(getattr(job, "status", None)) or "").lower() == "cancel_requested"
    except Exception:
        logger.exception("[admin_analysis] cancellation check failed job_id=%s", job_id)
        return False
    finally:
        db.close()


def _cancel_admin_analysis_job_response(job_id: object) -> JSONResponse:
    db = SessionLocal()
    try:
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_id).first()
        if not job:
            return _error_response(404, "not_found", "Analysis job was not found.")
        files = _admin_analysis_job_files(db, job.id)
        _set_admin_analysis_job_canceled(db, job, files)
        db.commit()
        db.refresh(job)
        files = _admin_analysis_job_files(db, job.id)
        logger.info("[admin_analysis] financial processing canceled job_id=%s", job_id)
        return JSONResponse({"ok": True, "job": _admin_analysis_job_payload(job, files)})
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] failed to mark job canceled job_id=%s", job_id)
        return _error_response(500, "analysis_cancel_failed", "Unable to cancel analysis job.")
    finally:
        db.close()


def _set_admin_analysis_job_canceled(
    db: Any,
    job: AdminAnalysisJob,
    files: list[AdminAnalysisJobFile],
) -> None:
    now = datetime.now(timezone.utc)
    job.status = "canceled"
    job.current_step = "Canceled"
    job.canceled_at = now
    job.updated_at = now
    for file_record in files:
        file_status = (_clean_text(getattr(file_record, "status", None)) or "").lower()
        if file_status in {"queued", "processing", "intake_pending", "cancel_requested"}:
            file_record.status = "canceled"


def _update_admin_financial_processing_progress(
    job_id: object,
    job_file_id: object,
    progress_percent: int,
    current_step: str,
) -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_id).first()
        job_cancel_requested = False
        if job:
            job_cancel_requested = (
                (_clean_text(getattr(job, "status", None)) or "").lower() == "cancel_requested"
            )
            if not job_cancel_requested:
                job.status = "processing"
                job.progress_percent = progress_percent
                job.current_step = current_step
                if not getattr(job, "started_at", None):
                    job.started_at = now
                job.updated_at = now

        if job_file_id:
            job_file = (
                db.query(AdminAnalysisJobFile)
                .filter(AdminAnalysisJobFile.id == job_file_id)
                .first()
            )
            if job_file and not job_cancel_requested:
                if (_clean_text(getattr(job_file, "status", None)) or "").lower() != "canceled":
                    job_file.status = "processing"
                if not getattr(job_file, "started_at", None):
                    job_file.started_at = now

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] progress update failed job_id=%s", job_id)
    finally:
        db.close()


def _mark_admin_financial_processing_error(
    job_id: object,
    job_file_id: object,
    error_code: str,
    error_message: str,
) -> None:
    if not job_id:
        return

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_id).first()
        if job:
            job.status = "error"
            job.current_step = "Financial analysis failed"
            job.error_code = error_code
            job.error_message = error_message
            job.errored_at = now
            job.updated_at = now

        if job_file_id:
            job_file = (
                db.query(AdminAnalysisJobFile)
                .filter(AdminAnalysisJobFile.id == job_file_id)
                .first()
            )
            if job_file:
                job_file.status = "error"
                job_file.error_code = error_code
                job_file.error_message = error_message
                job_file.errored_at = now

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] failed to mark financial processing error job_id=%s", job_id)
    finally:
        db.close()


def _mark_admin_ar_processing_error(
    job_id: object,
    job_file_id: object,
    error_code: str,
    error_message: str,
) -> None:
    if not job_id:
        return

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_id).first()
        if job:
            job.status = "error"
            job.current_step = "AR analysis failed"
            job.error_code = error_code
            job.error_message = error_message
            job.errored_at = now
            job.updated_at = now

        if job_file_id:
            job_file = (
                db.query(AdminAnalysisJobFile)
                .filter(AdminAnalysisJobFile.id == job_file_id)
                .first()
            )
            if job_file:
                job_file.status = "error"
                job_file.error_code = error_code
                job_file.error_message = error_message
                job_file.errored_at = now

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] failed to mark AR processing error job_id=%s", job_id)
    finally:
        db.close()


def _mark_admin_claims_processing_error(
    job_id: object,
    job_file_id: object,
    error_code: str,
    error_message: str,
) -> None:
    if not job_id:
        return

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job_id).first()
        if job:
            job.status = "error"
            job.current_step = "Claims analysis failed"
            job.error_code = error_code
            job.error_message = error_message
            job.errored_at = now
            job.updated_at = now

        if job_file_id:
            job_file = (
                db.query(AdminAnalysisJobFile)
                .filter(AdminAnalysisJobFile.id == job_file_id)
                .first()
            )
            if job_file:
                job_file.status = "error"
                job_file.error_code = error_code
                job_file.error_message = error_message
                job_file.errored_at = now

        db.commit()
    except Exception:
        db.rollback()
        logger.exception("[admin_analysis] failed to mark Claims processing error job_id=%s", job_id)
    finally:
        db.close()


def _job_uuid_or_not_found(job_id: str) -> tuple[Optional[UUID], Optional[JSONResponse]]:
    try:
        return UUID(str(job_id)), None
    except (TypeError, ValueError):
        return None, _error_response(404, "not_found", "Analysis job was not found.")


def _admin_analysis_job_files(db: Any, job_id: object) -> list[AdminAnalysisJobFile]:
    return (
        db.query(AdminAnalysisJobFile)
        .filter(AdminAnalysisJobFile.job_id == job_id)
        .order_by(AdminAnalysisJobFile.created_at.asc())
        .all()
    )


def _admin_analysis_error_payload(record: object) -> Optional[dict[str, Optional[str]]]:
    error_code = _clean_text(getattr(record, "error_code", None))
    error_message = _clean_text(getattr(record, "error_message", None))
    if not error_code and not error_message:
        return None
    return {
        "code": error_code,
        "message": error_message,
    }


def _admin_analysis_job_payload(
    job: AdminAnalysisJob,
    files: list[AdminAnalysisJobFile],
) -> dict[str, Any]:
    return {
        "id": _id_text(getattr(job, "id", None)),
        "status": _clean_text(getattr(job, "status", None)),
        "progressPercent": _optional_int(getattr(job, "progress_percent", None)) or 0,
        "currentStep": _clean_text(getattr(job, "current_step", None)),
        "clientEmail": _clean_text(getattr(job, "client_email", None)),
        "firstName": _clean_text(getattr(job, "first_name", None)),
        "lastName": _clean_text(getattr(job, "last_name", None)),
        "officeName": _clean_text(getattr(job, "office_name", None)),
        "orgType": _clean_text(getattr(job, "org_type", None)),
        "phone": _clean_text(getattr(job, "phone", None)),
        "ghlCid": _clean_text(getattr(job, "ghl_cid", None)),
        "clientMode": _clean_text(getattr(job, "client_mode", None)),
        "analysisRunId": _clean_text(getattr(job, "analysis_run_id", None)),
        "submissionId": _id_text(getattr(job, "submission_id", None)),
        "createdByAdminUserId": _clean_text(getattr(job, "created_by_admin_user_id", None)),
        "createdAt": _iso_datetime(getattr(job, "created_at", None)),
        "startedAt": _iso_datetime(getattr(job, "started_at", None)),
        "completedAt": _iso_datetime(getattr(job, "completed_at", None)),
        "canceledAt": _iso_datetime(getattr(job, "canceled_at", None)),
        "erroredAt": _iso_datetime(getattr(job, "errored_at", None)),
        "updatedAt": _iso_datetime(getattr(job, "updated_at", None)),
        "error": _admin_analysis_error_payload(job),
        "files": [_admin_analysis_job_file_payload(file_record) for file_record in files],
    }


def _admin_analysis_job_file_payload(file_record: AdminAnalysisJobFile) -> dict[str, Any]:
    payload = {
        "id": _id_text(getattr(file_record, "id", None)),
        "jobId": _id_text(getattr(file_record, "job_id", None)),
        "toolName": _clean_text(getattr(file_record, "tool_name", None)),
        "originalFilename": _clean_text(getattr(file_record, "original_filename", None)),
        "contentType": _clean_text(getattr(file_record, "content_type", None)),
        "byteSize": _optional_int(getattr(file_record, "byte_size", None)),
        "uploadFileId": _id_text(getattr(file_record, "upload_file_id", None)),
        "uploadId": _id_text(getattr(file_record, "upload_id", None)),
        "status": _clean_text(getattr(file_record, "status", None)),
        "createdAt": _iso_datetime(getattr(file_record, "created_at", None)),
        "startedAt": _iso_datetime(getattr(file_record, "started_at", None)),
        "completedAt": _iso_datetime(getattr(file_record, "completed_at", None)),
        "erroredAt": _iso_datetime(getattr(file_record, "errored_at", None)),
        "processedAt": _iso_datetime(getattr(file_record, "processed_at", None)),
        "error": _admin_analysis_error_payload(file_record),
    }
    analysis_data = _json_text_payload(getattr(file_record, "analysis_data", None))
    if analysis_data is not None:
        payload["analysisData"] = analysis_data
    return payload


def _admin_financial_analysis_data_object(
    value: object,
) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    text = _clean_text(value)
    if not text:
        return {}, _error_response(
            409,
            "missing_analysis_data",
            "Financial analysis data is required before promotion.",
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}, _error_response(
            409,
            "invalid_analysis_data",
            "Financial analysis data must be valid JSON.",
        )
    if not isinstance(parsed, dict):
        return {}, _error_response(
            409,
            "invalid_analysis_data",
            "Financial analysis data must be a JSON object.",
        )

    required_keys = ("raw_analyses", "deduplicated_issues", "total_issue_count")
    missing_keys = [key for key in required_keys if key not in parsed]
    if missing_keys:
        return {}, _error_response(
            409,
            "invalid_analysis_data",
            "Financial analysis data is missing required fields.",
        )
    return parsed, None


def _legacy_financial_analysis_payload(analysis_data: dict[str, Any]) -> str:
    return json.dumps(
        {
            "raw_analyses": analysis_data["raw_analyses"],
            "deduplicated_issues": analysis_data["deduplicated_issues"],
            "total_issue_count": analysis_data["total_issue_count"],
            "all_trends": analysis_data.get("all_trends", []),
        }
    )


def _admin_ar_analysis_data_object(
    value: object,
) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    text = _clean_text(value)
    if not text:
        return {}, _error_response(
            409,
            "missing_analysis_data",
            "AR analysis data is required before promotion.",
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}, _error_response(
            409,
            "invalid_analysis_data",
            "AR analysis data must be valid JSON.",
        )
    if not isinstance(parsed, dict):
        return {}, _error_response(
            409,
            "invalid_analysis_data",
            "AR analysis data must be a JSON object.",
        )

    required_keys = ("raw_analyses", "deduplicated_issues", "total_issue_count")
    missing_keys = [key for key in required_keys if key not in parsed]
    if missing_keys:
        return {}, _error_response(
            409,
            "invalid_analysis_data",
            "AR analysis data is missing required fields.",
        )
    return parsed, None


def _legacy_ar_analysis_payload(analysis_data: dict[str, Any]) -> str:
    return json.dumps(
        {
            "raw_analyses": analysis_data["raw_analyses"],
            "deduplicated_issues": analysis_data["deduplicated_issues"],
            "total_issue_count": analysis_data["total_issue_count"],
            "all_trends": analysis_data.get("all_trends", []),
        }
    )


def _admin_claims_analysis_data_object(
    value: object,
) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    text = _clean_text(value)
    if not text:
        return {}, _error_response(
            409,
            "missing_analysis_data",
            "Claims analysis data is required before promotion.",
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}, _error_response(
            409,
            "invalid_analysis_data",
            "Claims analysis data must be valid JSON.",
        )
    if not isinstance(parsed, dict):
        return {}, _error_response(
            409,
            "invalid_analysis_data",
            "Claims analysis data must be a JSON object.",
        )

    required_keys = ("raw_analyses", "deduplicated_issues", "total_issue_count")
    missing_keys = [key for key in required_keys if key not in parsed]
    if missing_keys:
        return {}, _error_response(
            409,
            "invalid_analysis_data",
            "Claims analysis data is missing required fields.",
        )
    return parsed, None


def _legacy_claims_analysis_payload(analysis_data: dict[str, Any]) -> str:
    return json.dumps(
        {
            "raw_analyses": analysis_data["raw_analyses"],
            "deduplicated_issues": analysis_data["deduplicated_issues"],
            "total_issue_count": analysis_data["total_issue_count"],
            "all_trends": analysis_data.get("all_trends", []),
        }
    )


def _promote_financial_admin_job_records(
    *,
    db: Any,
    job: AdminAnalysisJob,
    job_file: AdminAnalysisJobFile,
    upload_file: UploadFileRecord,
    analysis_data: dict[str, Any],
    client_email: str,
) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    # Re-read promotion links in this transaction before creating legacy rows so
    # retries after partial failures do not create duplicate submissions/uploads.
    job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job.id).first()
    job_file = (
        db.query(AdminAnalysisJobFile)
        .filter(AdminAnalysisJobFile.id == job_file.id)
        .first()
    )
    upload_file = (
        db.query(UploadFileRecord)
        .filter(UploadFileRecord.id == upload_file.id)
        .first()
    )
    if not job or not job_file or not upload_file:
        return {}, _error_response(
            409,
            "promotion_source_missing",
            "Financial analysis promotion source records are missing.",
        )

    promoted = False
    now = datetime.now(timezone.utc)
    upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _upsert_user_for_financial_promotion(db, job, client_email)

    upload_from_job_file = _existing_upload_for_id(db, getattr(job_file, "upload_id", None))
    upload_from_upload_file = _existing_upload_for_id(db, getattr(upload_file, "upload_id", None))
    if (
        upload_from_job_file
        and upload_from_upload_file
        and str(upload_from_job_file.id) != str(upload_from_upload_file.id)
    ):
        return {}, _error_response(
            409,
            "promotion_link_conflict",
            "Financial analysis job has conflicting upload links.",
        )

    upload = upload_from_job_file or upload_from_upload_file
    if upload:
        upload_safe, upload_error = _validate_financial_promotion_upload(upload, client_email)
        if not upload_safe:
            return {}, upload_error

    submission_from_job = _existing_submission_for_id(db, getattr(job, "submission_id", None))
    submission_from_upload = _existing_submission_for_id(db, getattr(upload, "submission_id", None))
    if (
        submission_from_job
        and submission_from_upload
        and str(submission_from_job.id) != str(submission_from_upload.id)
    ):
        return {}, _error_response(
            409,
            "promotion_link_conflict",
            "Financial analysis job has conflicting submission links.",
        )

    submission = submission_from_job or submission_from_upload
    if submission:
        submission_safe, submission_error = _validate_financial_promotion_submission(
            submission,
            client_email,
        )
        if not submission_safe:
            return {}, submission_error

    if not submission:
        submission = ClientSubmission(
            user_email=client_email,
            first_name=_clean_text(getattr(job, "first_name", None)),
            last_name=_clean_text(getattr(job, "last_name", None)),
            office_name=_clean_text(getattr(job, "office_name", None)),
            org_type=_clean_text(getattr(job, "org_type", None)),
            phone=_clean_text(getattr(job, "phone", None)),
            submitted_at=now,
            source="admin",
            status="completed",
            completed_at=now,
            analysis_run_id=_clean_text(getattr(job, "analysis_run_id", None)),
            ghl_cid=_clean_text(getattr(job, "ghl_cid", None)),
        )
        db.add(submission)
        db.flush()
        promoted = True

    legacy_analysis_data = _legacy_financial_analysis_payload(analysis_data)
    if not upload:
        upload = Upload(
            file_name=(
                _clean_text(getattr(upload_file, "original_filename", None))
                or _clean_text(getattr(job_file, "original_filename", None))
            ),
            tool_name=ADMIN_ANALYSIS_FINANCIAL_TOOL_NAME,
            upload_time=upload_time,
            user_email=client_email,
            analysis_data=legacy_analysis_data,
            submission_id=submission.id,
        )
        db.add(upload)
        db.flush()
        promoted = True
    else:
        if not getattr(upload, "submission_id", None):
            upload.submission_id = submission.id
            promoted = True
        if not _clean_text(getattr(upload, "analysis_data", None)):
            upload.analysis_data = legacy_analysis_data
            promoted = True
        if not _clean_text(getattr(upload, "user_email", None)):
            upload.user_email = client_email
            promoted = True

    if not getattr(job, "submission_id", None) or str(job.submission_id) != str(submission.id):
        job.submission_id = submission.id
        job.updated_at = now
        promoted = True

    if not getattr(job_file, "upload_id", None) or str(job_file.upload_id) != str(upload.id):
        job_file.upload_id = upload.id
        promoted = True

    if not getattr(upload_file, "upload_id", None) or str(upload_file.upload_id) != str(upload.id):
        upload_file.upload_id = upload.id
        promoted = True

    return {
        "submission_id": _id_text(getattr(submission, "id", None)),
        "upload_id": _id_text(getattr(upload, "id", None)),
        "promoted": promoted,
    }, None


def _promote_ar_admin_job_records(
    *,
    db: Any,
    job: AdminAnalysisJob,
    job_file: AdminAnalysisJobFile,
    upload_file: UploadFileRecord,
    analysis_data: dict[str, Any],
    client_email: str,
) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    # Re-read promotion links in this transaction before creating legacy rows so
    # retries after partial failures do not create duplicate submissions/uploads.
    job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job.id).first()
    job_file = (
        db.query(AdminAnalysisJobFile)
        .filter(AdminAnalysisJobFile.id == job_file.id)
        .first()
    )
    upload_file = (
        db.query(UploadFileRecord)
        .filter(UploadFileRecord.id == upload_file.id)
        .first()
    )
    if not job or not job_file or not upload_file:
        return {}, _error_response(
            409,
            "promotion_source_missing",
            "AR analysis promotion source records are missing.",
        )

    promoted = False
    now = datetime.now(timezone.utc)
    upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _upsert_user_for_financial_promotion(db, job, client_email)

    upload_from_job_file = _existing_upload_for_id(db, getattr(job_file, "upload_id", None))
    upload_from_upload_file = _existing_upload_for_id(db, getattr(upload_file, "upload_id", None))
    if (
        upload_from_job_file
        and upload_from_upload_file
        and str(upload_from_job_file.id) != str(upload_from_upload_file.id)
    ):
        return {}, _error_response(
            409,
            "promotion_link_conflict",
            "AR analysis job has conflicting upload links.",
        )

    upload = upload_from_job_file or upload_from_upload_file
    if upload:
        upload_safe, upload_error = _validate_ar_promotion_upload(upload, client_email)
        if not upload_safe:
            return {}, upload_error

    submission_from_job = _existing_submission_for_id(db, getattr(job, "submission_id", None))
    submission_from_upload = _existing_submission_for_id(db, getattr(upload, "submission_id", None))
    if (
        submission_from_job
        and submission_from_upload
        and str(submission_from_job.id) != str(submission_from_upload.id)
    ):
        return {}, _error_response(
            409,
            "promotion_link_conflict",
            "AR analysis job has conflicting submission links.",
        )

    submission = submission_from_job or submission_from_upload
    if submission:
        submission_safe, submission_error = _validate_ar_promotion_submission(
            submission,
            client_email,
        )
        if not submission_safe:
            return {}, submission_error

    if not submission:
        submission = ClientSubmission(
            user_email=client_email,
            first_name=_clean_text(getattr(job, "first_name", None)),
            last_name=_clean_text(getattr(job, "last_name", None)),
            office_name=_clean_text(getattr(job, "office_name", None)),
            org_type=_clean_text(getattr(job, "org_type", None)),
            phone=_clean_text(getattr(job, "phone", None)),
            submitted_at=now,
            source="admin",
            status="completed",
            completed_at=now,
            analysis_run_id=_clean_text(getattr(job, "analysis_run_id", None)),
            ghl_cid=_clean_text(getattr(job, "ghl_cid", None)),
        )
        db.add(submission)
        db.flush()
        promoted = True

    legacy_analysis_data = _legacy_ar_analysis_payload(analysis_data)
    if not upload:
        upload = Upload(
            file_name=(
                _clean_text(getattr(upload_file, "original_filename", None))
                or _clean_text(getattr(job_file, "original_filename", None))
            ),
            tool_name=ADMIN_ANALYSIS_AR_TOOL_NAME,
            upload_time=upload_time,
            user_email=client_email,
            analysis_data=legacy_analysis_data,
            submission_id=submission.id,
        )
        db.add(upload)
        db.flush()
        promoted = True
    else:
        if not getattr(upload, "submission_id", None):
            upload.submission_id = submission.id
            promoted = True
        if not _clean_text(getattr(upload, "analysis_data", None)):
            upload.analysis_data = legacy_analysis_data
            promoted = True
        if not _clean_text(getattr(upload, "user_email", None)):
            upload.user_email = client_email
            promoted = True

    if not getattr(job, "submission_id", None) or str(job.submission_id) != str(submission.id):
        job.submission_id = submission.id
        job.updated_at = now
        promoted = True

    if not getattr(job_file, "upload_id", None) or str(job_file.upload_id) != str(upload.id):
        job_file.upload_id = upload.id
        promoted = True

    if not getattr(upload_file, "upload_id", None) or str(upload_file.upload_id) != str(upload.id):
        upload_file.upload_id = upload.id
        promoted = True

    return {
        "submission_id": _id_text(getattr(submission, "id", None)),
        "upload_id": _id_text(getattr(upload, "id", None)),
        "promoted": promoted,
    }, None


def _promote_claims_admin_job_records(
    *,
    db: Any,
    job: AdminAnalysisJob,
    job_file: AdminAnalysisJobFile,
    upload_file: UploadFileRecord,
    analysis_data: dict[str, Any],
    client_email: str,
) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    # Re-read promotion links in this transaction before creating legacy rows so
    # retries after partial failures do not create duplicate submissions/uploads.
    job = db.query(AdminAnalysisJob).filter(AdminAnalysisJob.id == job.id).first()
    job_file = (
        db.query(AdminAnalysisJobFile)
        .filter(AdminAnalysisJobFile.id == job_file.id)
        .first()
    )
    upload_file = (
        db.query(UploadFileRecord)
        .filter(UploadFileRecord.id == upload_file.id)
        .first()
    )
    if not job or not job_file or not upload_file:
        return {}, _error_response(
            409,
            "promotion_source_missing",
            "Claims analysis promotion source records are missing.",
        )

    promoted = False
    now = datetime.now(timezone.utc)
    upload_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    _upsert_user_for_financial_promotion(db, job, client_email)

    upload_from_job_file = _existing_upload_for_id(db, getattr(job_file, "upload_id", None))
    upload_from_upload_file = _existing_upload_for_id(db, getattr(upload_file, "upload_id", None))
    if (
        upload_from_job_file
        and upload_from_upload_file
        and str(upload_from_job_file.id) != str(upload_from_upload_file.id)
    ):
        return {}, _error_response(
            409,
            "promotion_link_conflict",
            "Claims analysis job has conflicting upload links.",
        )

    upload = upload_from_job_file or upload_from_upload_file
    if upload:
        upload_safe, upload_error = _validate_claims_promotion_upload(upload, client_email)
        if not upload_safe:
            return {}, upload_error

    submission_from_job = _existing_submission_for_id(db, getattr(job, "submission_id", None))
    submission_from_upload = _existing_submission_for_id(db, getattr(upload, "submission_id", None))
    if (
        submission_from_job
        and submission_from_upload
        and str(submission_from_job.id) != str(submission_from_upload.id)
    ):
        return {}, _error_response(
            409,
            "promotion_link_conflict",
            "Claims analysis job has conflicting submission links.",
        )

    submission = submission_from_job or submission_from_upload
    if submission:
        submission_safe, submission_error = _validate_claims_promotion_submission(
            submission,
            client_email,
        )
        if not submission_safe:
            return {}, submission_error

    if not submission:
        submission = ClientSubmission(
            user_email=client_email,
            first_name=_clean_text(getattr(job, "first_name", None)),
            last_name=_clean_text(getattr(job, "last_name", None)),
            office_name=_clean_text(getattr(job, "office_name", None)),
            org_type=_clean_text(getattr(job, "org_type", None)),
            phone=_clean_text(getattr(job, "phone", None)),
            submitted_at=now,
            source="admin",
            status="completed",
            completed_at=now,
            analysis_run_id=_clean_text(getattr(job, "analysis_run_id", None)),
            ghl_cid=_clean_text(getattr(job, "ghl_cid", None)),
        )
        db.add(submission)
        db.flush()
        promoted = True

    legacy_analysis_data = _legacy_claims_analysis_payload(analysis_data)
    if not upload:
        upload = Upload(
            file_name=(
                _clean_text(getattr(upload_file, "original_filename", None))
                or _clean_text(getattr(job_file, "original_filename", None))
            ),
            tool_name=ADMIN_ANALYSIS_CLAIMS_TOOL_NAME,
            upload_time=upload_time,
            user_email=client_email,
            analysis_data=legacy_analysis_data,
            submission_id=submission.id,
        )
        db.add(upload)
        db.flush()
        promoted = True
    else:
        if not getattr(upload, "submission_id", None):
            upload.submission_id = submission.id
            promoted = True
        if not _clean_text(getattr(upload, "analysis_data", None)):
            upload.analysis_data = legacy_analysis_data
            promoted = True
        if not _clean_text(getattr(upload, "user_email", None)):
            upload.user_email = client_email
            promoted = True

    if not getattr(job, "submission_id", None) or str(job.submission_id) != str(submission.id):
        job.submission_id = submission.id
        job.updated_at = now
        promoted = True

    if not getattr(job_file, "upload_id", None) or str(job_file.upload_id) != str(upload.id):
        job_file.upload_id = upload.id
        promoted = True

    if not getattr(upload_file, "upload_id", None) or str(upload_file.upload_id) != str(upload.id):
        upload_file.upload_id = upload.id
        promoted = True

    return {
        "submission_id": _id_text(getattr(submission, "id", None)),
        "upload_id": _id_text(getattr(upload, "id", None)),
        "promoted": promoted,
    }, None


def _upsert_user_for_financial_promotion(
    db: Any,
    job: AdminAnalysisJob,
    client_email: str,
) -> User:
    user = db.query(User).filter(func.lower(User.email) == client_email).first()
    user_fields = {
        "first_name": _clean_text(getattr(job, "first_name", None)),
        "last_name": _clean_text(getattr(job, "last_name", None)),
        "office_name": _clean_text(getattr(job, "office_name", None)),
        "org_type": _clean_text(getattr(job, "org_type", None)),
        "phone": _clean_text(getattr(job, "phone", None)),
    }
    if user:
        user.email = client_email
        for field_name, value in user_fields.items():
            if value:
                setattr(user, field_name, value)
        return user

    user = User(
        email=client_email,
        first_name=user_fields["first_name"],
        last_name=user_fields["last_name"],
        office_name=user_fields["office_name"],
        org_type=user_fields["org_type"],
        phone=user_fields["phone"],
    )
    db.add(user)
    db.flush()
    return user


def _existing_upload_for_id(db: Any, upload_id: object) -> Optional[Upload]:
    if not upload_id:
        return None
    return db.query(Upload).filter(Upload.id == upload_id).first()


def _existing_submission_for_id(db: Any, submission_id: object) -> Optional[ClientSubmission]:
    if not submission_id:
        return None
    return db.query(ClientSubmission).filter(ClientSubmission.id == submission_id).first()


def _validate_financial_promotion_upload(
    upload: Upload,
    client_email: str,
) -> tuple[bool, Optional[JSONResponse]]:
    upload_email = (_clean_text(getattr(upload, "user_email", None)) or "").lower()
    if upload_email and upload_email != client_email:
        return False, _error_response(
            409,
            "promotion_link_conflict",
            "Existing upload belongs to a different client.",
        )

    tool_name = _clean_text(getattr(upload, "tool_name", None))
    if tool_name and tool_name != ADMIN_ANALYSIS_FINANCIAL_TOOL_NAME:
        return False, _error_response(
            409,
            "promotion_link_conflict",
            "Existing upload is not a Financial Analyzer upload.",
        )
    return True, None


def _validate_ar_promotion_upload(
    upload: Upload,
    client_email: str,
) -> tuple[bool, Optional[JSONResponse]]:
    upload_email = (_clean_text(getattr(upload, "user_email", None)) or "").lower()
    if upload_email and upload_email != client_email:
        return False, _error_response(
            409,
            "promotion_link_conflict",
            "Existing upload belongs to a different client.",
        )

    tool_name = _clean_text(getattr(upload, "tool_name", None))
    if tool_name and tool_name != ADMIN_ANALYSIS_AR_TOOL_NAME:
        return False, _error_response(
            409,
            "promotion_link_conflict",
            "Existing upload is not an AR Analyzer upload.",
        )
    return True, None


def _validate_claims_promotion_upload(
    upload: Upload,
    client_email: str,
) -> tuple[bool, Optional[JSONResponse]]:
    upload_email = (_clean_text(getattr(upload, "user_email", None)) or "").lower()
    if upload_email and upload_email != client_email:
        return False, _error_response(
            409,
            "promotion_link_conflict",
            "Existing upload belongs to a different client.",
        )

    tool_name = _clean_text(getattr(upload, "tool_name", None))
    if tool_name and tool_name != ADMIN_ANALYSIS_CLAIMS_TOOL_NAME:
        return False, _error_response(
            409,
            "promotion_link_conflict",
            "Existing upload is not an Insurance Claim Analyzer upload.",
        )
    return True, None


def _validate_financial_promotion_submission(
    submission: ClientSubmission,
    client_email: str,
) -> tuple[bool, Optional[JSONResponse]]:
    submission_email = (_clean_text(getattr(submission, "user_email", None)) or "").lower()
    if submission_email and submission_email != client_email:
        return False, _error_response(
            409,
            "promotion_link_conflict",
            "Existing submission belongs to a different client.",
        )
    return True, None


def _validate_ar_promotion_submission(
    submission: ClientSubmission,
    client_email: str,
) -> tuple[bool, Optional[JSONResponse]]:
    submission_email = (_clean_text(getattr(submission, "user_email", None)) or "").lower()
    if submission_email and submission_email != client_email:
        return False, _error_response(
            409,
            "promotion_link_conflict",
            "Existing submission belongs to a different client.",
        )
    return True, None


def _validate_claims_promotion_submission(
    submission: ClientSubmission,
    client_email: str,
) -> tuple[bool, Optional[JSONResponse]]:
    submission_email = (_clean_text(getattr(submission, "user_email", None)) or "").lower()
    if submission_email and submission_email != client_email:
        return False, _error_response(
            409,
            "promotion_link_conflict",
            "Existing submission belongs to a different client.",
        )
    return True, None


def _pdf_generator_submission_payload(submission: ClientSubmission) -> dict[str, Any]:
    return {
        "id": _id_text(getattr(submission, "id", None)),
        "clientEmail": _clean_text(getattr(submission, "user_email", None)),
        "firstName": _clean_text(getattr(submission, "first_name", None)),
        "lastName": _clean_text(getattr(submission, "last_name", None)),
        "officeName": _clean_text(getattr(submission, "office_name", None)),
        "orgType": _clean_text(getattr(submission, "org_type", None)),
        "phone": _clean_text(getattr(submission, "phone", None)),
        "source": _clean_text(getattr(submission, "source", None)),
        "status": _clean_text(getattr(submission, "status", None)),
        "submittedAt": _iso_datetime(getattr(submission, "submitted_at", None)),
        "completedAt": _iso_datetime(getattr(submission, "completed_at", None)),
        "analysisRunId": _clean_text(getattr(submission, "analysis_run_id", None)),
    }


def _pdf_generator_upload_payload(upload: Upload) -> dict[str, Any]:
    analysis_payload = _pdf_generator_analysis_payload(getattr(upload, "analysis_data", None))
    pdf_url = _clean_text(getattr(upload, "pdf_url", None)) or ""
    report_path = _pdf_generator_report_path(pdf_url)
    signed_url, signed_warning = _pdf_generator_signed_url(report_path)
    warnings = []
    if _clean_text(getattr(upload, "analysis_data", None)) and analysis_payload is None:
        warnings.append("analysis_data_unreadable")
    if pdf_url and not report_path:
        warnings.append("report_path_unavailable")
    if signed_warning:
        warnings.append(signed_warning)

    return {
        "id": _id_text(getattr(upload, "id", None)),
        "fileName": _clean_text(getattr(upload, "file_name", None)),
        "toolName": _clean_text(getattr(upload, "tool_name", None)),
        "uploadTime": _clean_text(getattr(upload, "upload_time", None)),
        "clientEmail": _clean_text(getattr(upload, "user_email", None)),
        "submissionId": _id_text(getattr(upload, "submission_id", None)),
        "paid": bool(getattr(upload, "paid", False)),
        "analysis": {
            "hasAnalysisData": analysis_payload is not None,
            "opportunities": _pdf_generator_opportunities(analysis_payload or {}),
            "trends": _pdf_generator_trends(analysis_payload or {}),
            "keyTrends": _pdf_generator_key_trends(analysis_payload or {}),
        },
        "pdf": {
            "pdfVersion": _optional_int(getattr(upload, "pdf_version", None)) or 0,
            "pdfUrl": pdf_url or None,
            "pdfGeneratedAt": _iso_datetime(getattr(upload, "pdf_generated_at", None)),
            "reportPath": report_path or None,
            "signedUrl": signed_url,
        },
        "warnings": warnings,
    }


def _pdf_generator_analysis_payload(value: object) -> Optional[dict[str, Any]]:
    if not value:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _pdf_generator_opportunities(payload: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    deduplicated = payload.get("deduplicated_issues", [])
    if not isinstance(deduplicated, list):
        return items

    for issue in deduplicated:
        if not isinstance(issue, dict):
            continue
        title = _clean_text(issue.get("title")) or ""
        impact = _clean_text(issue.get("impact")) or ""
        recommendation = _clean_text(issue.get("recommendation")) or ""
        if title or impact or recommendation:
            items.append(
                {
                    "title": title,
                    "impact": impact,
                    "recommendation": recommendation,
                }
            )
    return items


def _pdf_generator_trends(payload: dict[str, Any]) -> list[str]:
    items: list[str] = []
    trends = payload.get("all_trends", [])
    if not isinstance(trends, list):
        return items

    for trend in trends:
        if isinstance(trend, dict):
            text = _clean_text(trend.get("text")) or ""
        else:
            text = _clean_text(trend) or ""
        if text:
            items.append(text)
    return items


def _pdf_generator_key_trends(payload: dict[str, Any]) -> list[str]:
    if not payload:
        return []

    key_trends: list[str] = []
    seen: set[str] = set()

    trends = payload.get("all_trends", [])
    if isinstance(trends, list):
        for trend in trends:
            if isinstance(trend, dict):
                text = _clean_text(trend.get("text"))
            else:
                text = _clean_text(trend)
            if not text:
                continue
            normalized = text.lower()
            if normalized in seen:
                continue
            if any(char.isdigit() for char in text) or "%" in text:
                key_trends.append(text)
                seen.add(normalized)
            if len(key_trends) >= 3:
                break

    issues = payload.get("deduplicated_issues", [])
    if isinstance(issues, list):
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            title = _clean_text(issue.get("title")) or ""
            impact = _clean_text(issue.get("impact")) or ""
            if not title:
                continue
            normalized = title.lower()
            if normalized in seen:
                continue
            text = f"{title}: {impact}" if impact and len(impact) > 20 else title
            key_trends.append(text)
            seen.add(normalized)
            if len(key_trends) >= 5:
                break

    return key_trends[:5]


def _pdf_generator_report_path(pdf_url: str, bucket: str = "consulting-uploads") -> str:
    if not pdf_url:
        return ""
    if pdf_url.startswith("reports/"):
        return pdf_url
    if pdf_url.startswith(f"{bucket}/"):
        return pdf_url[len(bucket) + 1:]
    try:
        parsed = urlparse(pdf_url)
        path = unquote(parsed.path or "")
    except Exception:
        path = pdf_url
    markers = [
        f"/storage/v1/object/public/{bucket}/",
        f"/storage/v1/object/sign/{bucket}/",
        f"/storage/v1/object/{bucket}/",
        f"/{bucket}/",
    ]
    for marker in markers:
        if marker in path:
            return path.split(marker, 1)[1]
    return ""


def _pdf_generator_signed_url(path: str, expires_in: int = 3600) -> tuple[Optional[str], Optional[str]]:
    if not path:
        return None, None
    client = _get_supabase_admin_client()
    if not client:
        logger.warning("[admin_pdf_generator] signed url skipped; storage client unavailable path=%s", path)
        return None, "signed_url_unavailable"
    try:
        response = client.storage.from_("consulting-uploads").create_signed_url(path, expires_in)
    except Exception:
        logger.warning("[admin_pdf_generator] signed url failed path=%s", path, exc_info=True)
        return None, "signed_url_unavailable"

    signed_url = ""
    if isinstance(response, dict):
        signed_url = response.get("signedURL") or response.get("signedUrl") or ""
    elif isinstance(response, str):
        signed_url = response
    if not signed_url:
        return None, "signed_url_unavailable"
    return signed_url, None


def _validate_pdf_generation_body(
    body: dict[str, Any],
) -> tuple[UUID, list[dict[str, str]], list[str], list[str], str, Optional[JSONResponse]]:
    upload_id_text = _clean_text(body.get("uploadId"))
    if not upload_id_text:
        return UUID(int=0), [], [], [], "", _error_response(400, "missing_upload_id", "uploadId is required.")
    try:
        upload_id = UUID(upload_id_text)
    except ValueError:
        return UUID(int=0), [], [], [], "", _error_response(400, "invalid_upload_id", "uploadId must be a valid UUID.")

    opportunities_value = body.get("opportunities", [])
    trends_value = body.get("trends", [])
    key_trends_value = body.get("keyTrends", [])
    additional_notes, notes_error = _pdf_generation_text(
        body.get("additionalNotes", ""),
        "additionalNotes",
        6000,
        allow_empty=True,
    )
    if notes_error:
        return UUID(int=0), [], [], [], "", notes_error

    opportunities, opportunities_error = _pdf_generation_opportunities(opportunities_value)
    if opportunities_error:
        return UUID(int=0), [], [], [], "", opportunities_error

    trends, trends_error = _pdf_generation_text_list(
        trends_value,
        "trends",
        max_items=20,
        max_length=4000,
    )
    if trends_error:
        return UUID(int=0), [], [], [], "", trends_error

    key_trends, key_trends_error = _pdf_generation_text_list(
        key_trends_value,
        "keyTrends",
        max_items=10,
        max_length=4000,
    )
    if key_trends_error:
        return UUID(int=0), [], [], [], "", key_trends_error

    has_content = bool(opportunities or trends or key_trends or additional_notes)
    if not has_content:
        return UUID(int=0), [], [], [], "", _error_response(
            400,
            "missing_pdf_content",
            "At least one opportunity, trend, key trend, or note is required.",
        )

    return upload_id, opportunities, trends, key_trends, additional_notes, None


def _pdf_generation_opportunities(value: object) -> tuple[list[dict[str, str]], Optional[JSONResponse]]:
    if value is None:
        value = []
    if not isinstance(value, list):
        return [], _error_response(400, "invalid_opportunities", "opportunities must be a list.")
    if len(value) > 20:
        return [], _error_response(400, "too_many_opportunities", "opportunities must include 20 items or fewer.")

    opportunities: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return [], _error_response(400, "invalid_opportunity", "Each opportunity must be an object.")

        title, title_error = _pdf_generation_text(
            item.get("title", ""),
            f"opportunities[{index}].title",
            4000,
            allow_empty=True,
        )
        if title_error:
            return [], title_error
        impact, impact_error = _pdf_generation_text(
            item.get("impact", ""),
            f"opportunities[{index}].impact",
            4000,
            allow_empty=True,
        )
        if impact_error:
            return [], impact_error
        recommendation, recommendation_error = _pdf_generation_text(
            item.get("recommendation", ""),
            f"opportunities[{index}].recommendation",
            4000,
            allow_empty=True,
        )
        if recommendation_error:
            return [], recommendation_error

        if title or impact or recommendation:
            opportunities.append(
                {
                    "title": title,
                    "impact": impact,
                    "recommendation": recommendation,
                }
            )

    return opportunities, None


def _pdf_generation_text_list(
    value: object,
    field_name: str,
    *,
    max_items: int,
    max_length: int,
) -> tuple[list[str], Optional[JSONResponse]]:
    if value is None:
        value = []
    if not isinstance(value, list):
        return [], _error_response(400, f"invalid_{field_name}", f"{field_name} must be a list.")
    if len(value) > max_items:
        return [], _error_response(
            400,
            f"too_many_{field_name}",
            f"{field_name} must include {max_items} items or fewer.",
        )

    items: list[str] = []
    for index, item in enumerate(value):
        text, text_error = _pdf_generation_text(
            item,
            f"{field_name}[{index}]",
            max_length,
            allow_empty=True,
        )
        if text_error:
            return [], text_error
        if text:
            items.append(text)
    return items, None


def _pdf_generation_text(
    value: object,
    field_name: str,
    max_length: int,
    *,
    allow_empty: bool,
) -> tuple[str, Optional[JSONResponse]]:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        return "", _error_response(400, "invalid_pdf_content", f"{field_name} must be text.")

    if not text and not allow_empty:
        return "", _error_response(400, "missing_pdf_content", f"{field_name} is required.")
    if len(text) > max_length:
        return "", _error_response(
            400,
            "pdf_content_too_long",
            f"{field_name} must be {max_length} characters or fewer.",
        )
    return text, None


def _checkout_related_uploads_by_session(
    db: Any,
    sessions: list[StripeCheckoutSession],
) -> dict[str, list[Upload]]:
    session_ids = [
        getattr(session, "id")
        for session in sessions
        if getattr(session, "id", None)
    ]
    related_uploads_by_session_id = {
        str(session_id): []
        for session_id in session_ids
    }
    if not session_ids:
        return related_uploads_by_session_id

    link_rows = (
        db.query(StripeCheckoutSessionUpload, Upload)
        .join(Upload, Upload.id == StripeCheckoutSessionUpload.upload_id)
        .filter(StripeCheckoutSessionUpload.checkout_session_id.in_(session_ids))
        .order_by(StripeCheckoutSessionUpload.created_at.asc())
        .all()
    )
    for link, upload in link_rows:
        session_id = _id_text(getattr(link, "checkout_session_id", None))
        if session_id:
            related_uploads_by_session_id.setdefault(session_id, []).append(upload)

    legacy_upload_ids = [
        getattr(session, "upload_id")
        for session in sessions
        if getattr(session, "upload_id", None)
        and not related_uploads_by_session_id.get(str(getattr(session, "id", "")))
    ]
    if not legacy_upload_ids:
        return related_uploads_by_session_id

    legacy_upload_rows = db.query(Upload).filter(Upload.id.in_(legacy_upload_ids)).all()
    legacy_uploads_by_id = {
        str(getattr(upload, "id")): upload
        for upload in legacy_upload_rows
        if getattr(upload, "id", None)
    }
    for session in sessions:
        session_id = _id_text(getattr(session, "id", None))
        legacy_upload_id = _id_text(getattr(session, "upload_id", None))
        if (
            session_id
            and legacy_upload_id
            and not related_uploads_by_session_id.get(session_id)
            and legacy_upload_id in legacy_uploads_by_id
        ):
            related_uploads_by_session_id[session_id] = [legacy_uploads_by_id[legacy_upload_id]]

    return related_uploads_by_session_id


def _checkout_session_payload(
    session: StripeCheckoutSession,
    related_uploads: Optional[list[Upload]] = None,
) -> dict[str, Any]:
    related_uploads = related_uploads or []
    upload_ids = []
    seen_upload_ids = set()
    for upload in related_uploads:
        upload_id = _id_text(getattr(upload, "id", None))
        if upload_id and upload_id not in seen_upload_ids:
            upload_ids.append(upload_id)
            seen_upload_ids.add(upload_id)

    legacy_upload_id = _id_text(getattr(session, "upload_id", None))
    if legacy_upload_id and legacy_upload_id not in seen_upload_ids:
        upload_ids.append(legacy_upload_id)

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
        "uploadId": legacy_upload_id,
        "uploadIds": upload_ids,
        "relatedUploads": [_upload_payload(upload) for upload in related_uploads],
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


def _secure_upload_file_payload(upload_file: UploadPortalFile) -> dict[str, Any]:
    bucket = _clean_text(getattr(upload_file, "gcs_bucket", None))
    object_name = _clean_text(getattr(upload_file, "object_name", None))
    gs_path = f"gs://{bucket}/{object_name}" if bucket and object_name else None
    console_url = None
    if bucket and object_name:
        encoded_object = quote(object_name, safe="/")
        console_url = (
            "https://console.cloud.google.com/storage/browser/_details/"
            f"{bucket}/{encoded_object}"
        )

    byte_size_value = getattr(upload_file, "byte_size", None)
    byte_size = None
    if byte_size_value is not None:
        try:
            byte_size = int(byte_size_value)
        except (TypeError, ValueError):
            byte_size = None

    return {
        "id": _id_text(getattr(upload_file, "id", None)),
        "requestId": _id_text(getattr(upload_file, "request_id", None)),
        "sessionId": _id_text(getattr(upload_file, "session_id", None)),
        "userId": _id_text(getattr(upload_file, "user_id", None)),
        "userEmail": _clean_text(getattr(upload_file, "user_email", None)),
        "originalFilename": _clean_text(getattr(upload_file, "original_filename", None)),
        "contentType": _clean_text(getattr(upload_file, "content_type", None)),
        "byteSize": byte_size,
        "gcsBucket": bucket,
        "objectName": object_name,
        "gsPath": gs_path,
        "consoleUrl": console_url,
        "createdAt": _iso_datetime(getattr(upload_file, "created_at", None)),
        "completedAt": _iso_datetime(getattr(upload_file, "completed_at", None)),
    }


def _admin_date_filter_start(value: object, field_name: str) -> tuple[Optional[datetime], Optional[JSONResponse]]:
    text = _clean_text(value)
    if not text:
        return None, None
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc), None
    except ValueError:
        return None, _error_response(400, f"invalid_{field_name}", f"{field_name} must use YYYY-MM-DD.")


def _request_client_ip(request: Request) -> Optional[str]:
    forwarded_for = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    if forwarded_for:
        return forwarded_for
    if request.client and request.client.host:
        return request.client.host
    return None


def _portal_token_ttl_minutes() -> int:
    try:
        return max(1, int(os.getenv("PORTAL_TOKEN_TTL_MINUTES", "60")))
    except ValueError:
        return 60


def _id_text(value: object) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _json_text_payload(value: object) -> Optional[Any]:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


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


def _required_admin_access_email(value: object) -> tuple[str, Optional[JSONResponse]]:
    email = _clean_text(value)
    if not email:
        return "", _error_response(400, "missing_email", "email is required.")
    email = email.lower()
    if len(email) > 254 or "@" not in email:
        return "", _error_response(400, "invalid_email", "email must be a valid email.")
    return email, None


def _required_admin_display_name(value: object) -> tuple[str, Optional[JSONResponse]]:
    display_name = _clean_text(value)
    if not display_name:
        return "", _error_response(400, "missing_name", "name is required.")
    if len(display_name) > 160:
        return "", _error_response(400, "invalid_name", "name must be 160 characters or fewer.")
    return display_name, None


def _optional_admin_display_name(value: object) -> tuple[str, Optional[JSONResponse]]:
    display_name = _clean_text(value)
    if not display_name:
        return "", _error_response(400, "missing_name", "name is required when provided.")
    if len(display_name) > 160:
        return "", _error_response(400, "invalid_name", "name must be 160 characters or fewer.")
    return display_name, None


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


def _required_admin_user_role(value: object) -> tuple[str, Optional[JSONResponse]]:
    role = (_clean_text(value) or "").lower()
    if not role:
        return "", _error_response(400, "missing_role", "role is required.")
    if role not in ADMIN_USER_WRITE_ROLES:
        return "", _error_response(
            400,
            "invalid_role",
            "role must be super_admin, admin, analyst, billing_admin, or viewer.",
        )
    return role, None


def _required_admin_user_status(value: object) -> tuple[str, Optional[JSONResponse]]:
    status = (_clean_text(value) or "").lower()
    if not status:
        return "", _error_response(400, "invalid_status", "status must be active or inactive.")
    if status not in {"active", "inactive"}:
        return "", _error_response(400, "invalid_status", "status must be active or inactive.")
    return status, None


def _required_uuid(value: object, field_name: str) -> tuple[UUID, Optional[JSONResponse]]:
    text = _clean_text(value)
    if not text:
        return UUID(int=0), _error_response(400, f"missing_{field_name}", f"{field_name} is required.")
    try:
        return UUID(text), None
    except ValueError:
        return UUID(int=0), _error_response(400, f"invalid_{field_name}", f"{field_name} must be a valid UUID.")


def _optional_uuid(value: object, field_name: str) -> tuple[Optional[UUID], Optional[JSONResponse]]:
    text = _clean_text(value)
    if not text or text.lower() == "null":
        return None, None
    try:
        return UUID(text), None
    except ValueError:
        return None, _error_response(400, f"invalid_{field_name}", f"{field_name} must be a valid UUID.")


def _checkout_upload_ids_from_body(body: dict[str, Any]) -> tuple[list[UUID], Optional[JSONResponse]]:
    parsed_upload_ids: list[UUID] = []
    upload_id, validation_error = _optional_uuid(body.get("uploadId"), "uploadId")
    if validation_error:
        return [], validation_error
    if upload_id:
        parsed_upload_ids.append(upload_id)

    raw_upload_ids = body.get("uploadIds")
    if raw_upload_ids is not None:
        if not isinstance(raw_upload_ids, list):
            return [], _error_response(400, "invalid_uploadIds", "uploadIds must be an array of UUID strings.")
        for raw_upload_id in raw_upload_ids:
            selected_upload_id, validation_error = _required_uuid(raw_upload_id, "uploadIds")
            if validation_error:
                return [], validation_error
            parsed_upload_ids.append(selected_upload_id)

    deduped_upload_ids = []
    seen_upload_ids = set()
    for selected_upload_id in parsed_upload_ids:
        selected_upload_id_text = str(selected_upload_id)
        if selected_upload_id_text in seen_upload_ids:
            continue
        deduped_upload_ids.append(selected_upload_id)
        seen_upload_ids.add(selected_upload_id_text)
    return deduped_upload_ids, None


def _validate_checkout_uploads(
    db: Any,
    upload_ids: list[UUID],
    client_email: str,
) -> tuple[list[Upload], Optional[JSONResponse]]:
    if not upload_ids:
        return [], None

    uploads = db.query(Upload).filter(Upload.id.in_(upload_ids)).all()
    uploads_by_id = {
        str(getattr(upload, "id")): upload
        for upload in uploads
        if getattr(upload, "id", None)
    }
    missing_upload_ids = [
        str(selected_upload_id)
        for selected_upload_id in upload_ids
        if str(selected_upload_id) not in uploads_by_id
    ]
    if missing_upload_ids:
        return [], _error_response(404, "upload_not_found", "One or more selected uploads could not be found.")

    mismatched_upload_ids = [
        str(selected_upload_id)
        for selected_upload_id in upload_ids
        if (_clean_text(getattr(uploads_by_id[str(selected_upload_id)], "user_email", None)) or "").lower()
        != client_email
    ]
    if mismatched_upload_ids:
        return [], _error_response(400, "upload_client_mismatch", "Selected uploads must belong to clientEmail.")

    return [uploads_by_id[str(selected_upload_id)] for selected_upload_id in upload_ids], None


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


def _admin_access_payload_for_user(
    user: dict[str, Any],
    admin_user: Optional[AdminUser] = None,
) -> dict[str, str]:
    user_id = str(user.get("id") or "")
    if admin_user is None:
        admin_user = _admin_user_for_user_id(user_id)
    admin_user_payload = _admin_user_payload(admin_user) if admin_user else None
    return {
        "id": user_id,
        "email": str(user.get("email") or ""),
        "role": (admin_user_payload or {}).get("role") or "admin",
        "status": (admin_user_payload or {}).get("status") or "active",
    }


def _admin_user_for_user_id(user_id: object) -> Optional[AdminUser]:
    try:
        normalized_user_id = UUID(str(user_id))
    except (TypeError, ValueError):
        return None

    db = SessionLocal()
    try:
        return db.query(AdminUser).filter(AdminUser.user_id == normalized_user_id).first()
    except Exception:
        logger.exception("[admin_api] admin user metadata lookup failed user_id=%s", user_id)
        return None
    finally:
        db.close()


def _admin_user_payload(admin_user: AdminUser) -> dict[str, Any]:
    return {
        "userId": _id_text(getattr(admin_user, "user_id", None)),
        "email": _clean_text(getattr(admin_user, "email", None)),
        "displayName": _clean_text(getattr(admin_user, "display_name", None)),
        "role": _clean_text(getattr(admin_user, "role", None)) or "admin",
        "status": _clean_text(getattr(admin_user, "status", None)) or "active",
        "createdAt": _iso_datetime(getattr(admin_user, "created_at", None)),
        "updatedAt": _iso_datetime(getattr(admin_user, "updated_at", None)),
    }


def _active_super_admin_count(db: Any) -> int:
    return int(
        db.query(func.count(AdminUser.user_id))
        .filter(func.lower(AdminUser.role) == "super_admin")
        .filter(func.lower(AdminUser.status) == "active")
        .filter(AdminUser.deactivated_at.is_(None))
        .scalar()
        or 0
    )


def _admin_role_permissions(role: object) -> set[str]:
    normalized_role = (_clean_text(role) or "").lower()
    return set(ADMIN_ROLE_PERMISSION_MAP.get(normalized_role, set()))


def _admin_dashboard_access_allows(admin_user: Optional[AdminUser]) -> bool:
    if not admin_user:
        return False
    role = (_clean_text(getattr(admin_user, "role", None)) or "").lower()
    status = (_clean_text(getattr(admin_user, "status", None)) or "active").lower()
    return role in ADMIN_API_DASHBOARD_ROLES and status == "active" and not getattr(admin_user, "deactivated_at", None)


def _admin_has_permission(admin_user: Optional[AdminUser], permission: str) -> bool:
    if not _admin_dashboard_access_allows(admin_user):
        return False
    return permission in _admin_role_permissions(getattr(admin_user, "role", None))


def _admin_permissions_payload(admin_user: Optional[AdminUser]) -> dict[str, bool]:
    return {
        "canReadClients": _admin_has_permission(admin_user, PERMISSION_CLIENTS_READ),
        "canReadBilling": _admin_has_permission(admin_user, PERMISSION_BILLING_READ),
        "canWriteBilling": _admin_has_permission(admin_user, PERMISSION_BILLING_WRITE),
        "canReadAnalysis": _admin_has_permission(admin_user, PERMISSION_ANALYSIS_READ),
        "canWriteAnalysis": _admin_has_permission(admin_user, PERMISSION_ANALYSIS_WRITE),
        "canReadPdf": _admin_has_permission(admin_user, PERMISSION_PDF_READ),
        "canGeneratePdf": _admin_has_permission(admin_user, PERMISSION_PDF_GENERATE),
        "canReadSecureUploads": _admin_has_permission(admin_user, PERMISSION_SECURE_UPLOADS_READ),
        "canWriteSecureUploads": _admin_has_permission(admin_user, PERMISSION_SECURE_UPLOADS_WRITE),
        "canReadAdminManagement": _admin_has_permission(admin_user, PERMISSION_ADMIN_MANAGEMENT_READ),
        "canManageAdminAccess": _admin_has_permission(admin_user, PERMISSION_ADMIN_MANAGEMENT_WRITE),
    }


def _admin_can_manage_access(admin_user: Optional[AdminUser]) -> bool:
    return _admin_has_permission(admin_user, PERMISSION_ADMIN_MANAGEMENT_WRITE)


def _require_dashboard_access(
    request: Request,
) -> tuple[dict[str, Any], Optional[AdminUser], Optional[JSONResponse]]:
    access_token = _bearer_token(request)
    if not access_token:
        return {}, None, _error_response(401, "unauthorized", "Authentication is required.")

    user = get_current_admin_user(access_token)
    if not user or not user.get("id"):
        return {}, None, _error_response(401, "unauthorized", "Authentication is invalid.")

    admin_user = _admin_user_for_user_id(user.get("id"))
    if not _admin_dashboard_access_allows(admin_user):
        logger.warning("[admin_api] dashboard access denied user_id=%s", user.get("id"))
        return {}, None, _error_response(403, "forbidden", "Admin dashboard access is required.")

    return user, admin_user, None


def _require_admin_permission(
    request: Request,
    permission: str,
) -> tuple[dict[str, Any], Optional[AdminUser], Optional[JSONResponse]]:
    user, admin_user, error_response = _require_dashboard_access(request)
    if error_response:
        return {}, None, error_response

    if not _admin_has_permission(admin_user, permission):
        logger.warning(
            "[admin_api] admin permission denied user_id=%s permission=%s",
            user.get("id"),
            permission,
        )
        return {}, None, _error_response(403, "forbidden", "You do not have permission to perform this action.")

    return user, admin_user, None


def _require_admin_access_manager(
    request: Request,
) -> tuple[dict[str, Any], Optional[AdminUser], Optional[JSONResponse]]:
    return _require_admin_permission(request, PERMISSION_ADMIN_MANAGEMENT_WRITE)


def _uuid_or_none(value: object) -> Optional[UUID]:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
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
