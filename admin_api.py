from __future__ import annotations

import logging
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional
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
    run_financial_csv_analysis,
)
from database import SessionLocal
from models import (
    AdminAnalysisJob,
    AdminAnalysisJobFile,
    BillingOverride,
    ClientSubmission,
    StripeCheckoutSession,
    StripeCustomer,
    StripeEvent,
    Upload,
    UploadFile as UploadFileRecord,
    User,
)
from supabase_utils import (
    _get_supabase_admin_client,
    get_current_admin_user,
    is_admin_user,
    persist_upload_file,
)

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="Admin API")

ADMIN_ANALYSIS_ALLOWED_TOOL_NAMES = {
    "Financial Analyzer",
    "AR Analyzer",
    "Insurance Claim Analyzer",
}
ADMIN_ANALYSIS_FINANCIAL_TOOL_NAME = "Financial Analyzer"
ADMIN_ANALYSIS_FINANCIAL_ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".pdf"}
ADMIN_ANALYSIS_READ_CHUNK_SIZE = 1024 * 1024
DEFAULT_ADMIN_ANALYSIS_MAX_FILE_MB = 15

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


@app.get("/api/admin/client-options")
def list_admin_client_options(
    request: Request,
    search: Optional[str] = None,
    limit: int = Query(75, ge=1),
) -> JSONResponse:
    _, error_response = _require_admin_user(request)
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
    admin_user, error_response = _require_admin_user(request)
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
    admin_user, error_response = _require_admin_user(request)
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


@app.get("/api/admin/analysis-jobs/{job_id}")
def get_admin_analysis_job(request: Request, job_id: str) -> JSONResponse:
    _, error_response = _require_admin_user(request)
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
    _, error_response = _require_admin_user(request)
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
    _, error_response = _require_admin_user(request)
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
        if status not in {"queued", "processing"}:
            return _error_response(
                409,
                "invalid_job_status",
                "Analysis job must be queued or processing.",
            )

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
        if _file_extension(original_filename) != ".csv":
            return _error_response(
                400,
                "unsupported_file_type",
                "DA-3B supports CSV Financial Analyzer processing only.",
            )

        bucket = _clean_text(getattr(upload_file, "bucket", None))
        object_path = _clean_text(getattr(upload_file, "object_path", None))
        now = datetime.now(timezone.utc)
        job.status = "processing"
        job.progress_percent = max(_optional_int(getattr(job, "progress_percent", None)) or 0, 20)
        job.current_step = "Downloading financial file"
        if not getattr(job, "started_at", None):
            job.started_at = now
        job.updated_at = now
        job.error_code = None
        job.error_message = None

        job_file.status = "processing"
        if not getattr(job_file, "started_at", None):
            job_file.started_at = now
        job_file.error_code = None
        job_file.error_message = None
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
            "Extracting CSV data",
        )
        data_input = extract_csv_text(file_bytes)
    except AdminFinancialProcessingError as exc:
        _mark_admin_financial_processing_error(job_uuid, job_file_id, exc.code, exc.message)
        return _error_response(400, exc.code, exc.message)
    except Exception:
        logger.exception("[admin_analysis] financial CSV extraction failed job_id=%s", job_uuid)
        _mark_admin_financial_processing_error(
            job_uuid,
            job_file_id,
            "csv_extract_failed",
            "Unable to extract CSV data.",
        )
        return _error_response(400, "csv_extract_failed", "Unable to extract CSV data.")

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
