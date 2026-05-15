from __future__ import annotations

import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Optional

import requests
from fastapi import BackgroundTasks, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logger = logging.getLogger("uvicorn.error")

ALLOWED_EXTENSIONS = {".pdf", ".csv", ".xlsx"}
ACKNOWLEDGEMENT_VERSION = "financial-only-v1"
DEFAULT_MAX_FILE_MB = 15
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = 3600
DEFAULT_RATE_LIMIT_MAX = 3
DEFAULT_PREFILL_RATE_LIMIT_MAX = 30
READ_CHUNK_SIZE = 1024 * 1024
CID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,128}$")
CANCELED_MESSAGE = "Analysis canceled. No results were saved."

app = FastAPI(title="Public Analyzer API")

allowed_origins = [
    origin.strip()
    for origin in os.getenv("ANALYZER_API_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = Lock()

rate_limit_hits: Dict[str, list[float]] = {}
rate_limit_lock = Lock()

prefill_rate_limit_hits: Dict[str, list[float]] = {}
prefill_rate_limit_lock = Lock()


@app.get("/")
def root() -> Dict[str, Any]:
    return {"ok": True, "service": "public-analyzer-api"}


@app.head("/")
def root_head() -> Response:
    return Response(status_code=200)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": "public-analyzer-api"}


@app.get("/api/public-analyzer/ghl-prefill")
def get_ghl_prefill(request: Request, cid: str) -> JSONResponse:
    cleaned_cid = cid.strip()
    if not _is_valid_cid(cleaned_cid):
        return _error_response(400, "validation_error", "Invalid contact identifier.")

    client_ip = _client_ip(request)
    if not _prefill_rate_limit_allows(client_ip):
        return _error_response(
            429,
            "rate_limited",
            "Too many prefill requests. Please try again later.",
        )

    contact, error_code = _fetch_ghl_contact(cleaned_cid)
    if error_code:
        if error_code == "not_configured":
            return _error_response(503, "not_configured", "GHL prefill is not configured.")
        return _error_response(404, "not_found", "Contact information could not be loaded.")

    if not _contact_matches_location(contact):
        return _error_response(404, "not_found", "Contact information could not be loaded.")

    response = _safe_prefill_response(cleaned_cid, contact)
    if not response["lockedFields"]:
        return _error_response(404, "not_found", "Contact information could not be loaded.")
    return JSONResponse(response)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    del request, exc
    return _error_response(422, "validation_error", "Invalid analyzer submission.")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    del request
    logger.exception("Unhandled public analyzer API error.")
    return _error_response(500, "internal_error", "Analyzer API error.")


@app.post("/api/public-analyzer/submissions")
async def create_public_analyzer_submission(
    request: Request,
    background_tasks: BackgroundTasks,
    first_name: str = Form(...),
    last_name: str = Form(...),
    office_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    org_type: str = Form(...),
    financial_only_acknowledgement: str = Form(...),
    cid: Optional[str] = Form(None),
    source_path: Optional[str] = Form(None),
    companyWebsite: Optional[str] = Form(None),
    file: UploadFile = File(...),
) -> JSONResponse:
    if (companyWebsite or "").strip():
        job_id = _create_job(status="completed")
        _log_job_status("completed", job_id)
        return JSONResponse({"ok": True, "job_id": job_id, "status": "completed"})

    client_ip = _client_ip(request)
    if not _rate_limit_allows(client_ip):
        return _error_response(
            429,
            "rate_limited",
            "Too many analyzer submissions. Please try again later.",
        )
    if not phone.strip():
        return _error_response(400, "validation_error", "Phone number is required.")
    if _parse_truthy_form_value(financial_only_acknowledgement) is not True:
        return _error_response(
            400,
            "validation_error",
            "Financial/practice operations acknowledgement is required.",
        )

    filename = (file.filename or "").strip()
    extension = _file_extension(filename)
    if extension not in ALLOWED_EXTENSIONS:
        return _error_response(400, "unsupported_file_type", "Unsupported file type.")

    try:
        file_bytes = await _read_upload_file(file)
    except ValueError as exc:
        return _error_response(400, str(exc), _error_message_for_code(str(exc)))
    finally:
        await file.close()

    job_id = _create_job(status="queued")
    _log_job_status("queued", job_id)
    background_tasks.add_task(
        _process_submission_job,
        job_id=job_id,
        first_name=first_name,
        last_name=last_name,
        office_name=office_name,
        email=email,
        phone=phone,
        org_type=org_type,
        financial_only_acknowledgement=True,
        acknowledgement_timestamp=datetime.now(timezone.utc),
        acknowledgement_ip=client_ip,
        acknowledgement_version=ACKNOWLEDGEMENT_VERSION,
        uploaded_file_bytes=file_bytes,
        original_filename=filename,
        content_type=file.content_type,
        cid=cid,
        source_path=source_path,
    )

    return JSONResponse({"ok": True, "job_id": job_id, "status": "queued"})


@app.get("/api/public-analyzer/submissions/{job_id}")
def get_public_analyzer_submission(job_id: str) -> JSONResponse:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return _error_response(404, "not_found", "Analyzer submission job was not found.")
        response = dict(job)

    response["ok"] = True
    response["job_id"] = job_id
    return JSONResponse(response)


@app.post("/api/public-analyzer/submissions/{job_id}/cancel")
def cancel_public_analyzer_submission(job_id: str) -> JSONResponse:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return _error_response(404, "not_found", "Analyzer submission job was not found.")

        status = str(job.get("status") or "")
        if status in {"completed", "error"}:
            return _error_response(409, "job_already_finished", "This analyzer job has already finished.")

        if status in {"cancel_requested", "canceled"}:
            response_payload = {
                "ok": True,
                "job_id": job_id,
                "status": status,
                "message": job.get("error_message") or CANCELED_MESSAGE,
            }

        elif bool(job.get("finalization_started")):
            return _error_response(
                409,
                "job_already_finalizing",
                "This analysis is already finalizing and may not be stopped.",
            )

        elif status in {"queued", "processing"}:
            now = time.time()
            job.update(
                {
                    "status": "cancel_requested",
                    "cancel_requested": True,
                    "error_code": None,
                    "error_message": "Cancel requested.",
                    "updated_at": now,
                }
            )
            response_payload = {
                "ok": True,
                "job_id": job_id,
                "status": "cancel_requested",
                "message": "Cancel requested.",
            }

        else:
            return _error_response(409, "job_already_finished", "This analyzer job has already finished.")

    return JSONResponse(response_payload)


def _process_submission_job(
    *,
    job_id: str,
    first_name: str,
    last_name: str,
    office_name: str,
    email: str,
    phone: str,
    org_type: str,
    financial_only_acknowledgement: bool,
    acknowledgement_timestamp: datetime,
    acknowledgement_ip: str,
    acknowledgement_version: str,
    uploaded_file_bytes: bytes,
    original_filename: str,
    content_type: Optional[str],
    cid: Optional[str],
    source_path: Optional[str],
) -> None:
    if _job_cancel_requested(job_id):
        _mark_job_canceled(job_id)
        _log_job_status("canceled", job_id)
        return

    _log_job_status("started", job_id)
    _update_job(job_id, status="processing")
    canceled_exc_type = None
    try:
        from public_analyzer_service import PublicAnalyzerCanceled, submit_public_analyzer_submission

        canceled_exc_type = PublicAnalyzerCanceled

        result = submit_public_analyzer_submission(
            first_name=first_name,
            last_name=last_name,
            office_name=office_name,
            email=email,
            org_type=org_type,
            phone=phone,
            financial_only_acknowledgement=financial_only_acknowledgement,
            acknowledgement_timestamp=acknowledgement_timestamp,
            acknowledgement_ip=acknowledgement_ip,
            acknowledgement_version=acknowledgement_version,
            require_public_api_metadata=True,
            uploaded_file_bytes=uploaded_file_bytes,
            original_filename=original_filename,
            content_type=content_type,
            cid=cid,
            source_path=source_path,
            cancel_checker=lambda: _job_cancel_requested(job_id),
            submission_created_callback=lambda submission_id: _update_job(
                job_id,
                submission_id=submission_id,
            ),
            finalization_started_callback=lambda: _mark_job_finalizing(job_id),
        )

        if result.get("status") == "canceled":
            _mark_job_canceled(
                job_id,
                submission_id=result.get("submission_id"),
                upload_id=result.get("upload_id"),
            )
            _log_job_status("canceled", job_id)
            return

        completed_status = "completed" if result.get("ok") else "error"
        _update_job(
            job_id,
            status=completed_status,
            submission_id=result.get("submission_id"),
            upload_id=result.get("upload_id"),
            error_code=result.get("error_code"),
            error_message=result.get("error_message"),
        )
        if completed_status == "completed":
            _log_job_status("completed", job_id)
        else:
            _log_job_status("failed", job_id)
    except Exception as exc:
        if canceled_exc_type is not None and isinstance(exc, canceled_exc_type):
            submission_id = getattr(exc, "submission_id", None) or _job_field(job_id, "submission_id")
            upload_id = getattr(exc, "upload_id", None)
            _mark_job_canceled(job_id, submission_id=submission_id, upload_id=upload_id)
            _log_job_status("canceled", job_id)
            return
        _log_job_status("failed", job_id)
        _update_job(
            job_id,
            status="error",
            error_code="analysis_failed",
            error_message="Analyzer job failed.",
        )


def _log_job_status(status: str, job_id: str) -> None:
    if status == "failed":
        logger.warning("public_analyzer_job status=%s job_id=%s", status, job_id)
        return
    logger.info("public_analyzer_job status=%s job_id=%s", status, job_id)


def _parse_truthy_form_value(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off", ""):
        return False
    return None


def _create_job(*, status: str) -> str:
    now = time.time()
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "status": status,
            "cancel_requested": False,
            "finalization_started": False,
            "submission_id": None,
            "upload_id": None,
            "error_code": None,
            "error_message": None,
            "canceled_at": None,
            "created_at": now,
            "updated_at": now,
        }
    return job_id


def _update_job(job_id: str, **updates: Any) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = time.time()


def _job_cancel_requested(job_id: str) -> bool:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return False
        status = str(job.get("status") or "")
        return bool(job.get("cancel_requested")) or status in {"cancel_requested", "canceled"}


def _job_field(job_id: str, field: str) -> Any:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return None
        return job.get(field)


def _mark_job_finalizing(job_id: str) -> bool:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return False
        status = str(job.get("status") or "")
        if bool(job.get("cancel_requested")) or status in {"cancel_requested", "canceled"}:
            return False
        job.update(
            {
                "finalization_started": True,
                "error_code": None,
                "error_message": "Finalizing results.",
                "updated_at": time.time(),
            }
        )
        return True


def _mark_job_canceled(
    job_id: str,
    *,
    submission_id: Optional[Any] = None,
    upload_id: Optional[Any] = None,
) -> None:
    updates: Dict[str, Any] = {
        "status": "canceled",
        "cancel_requested": True,
        "canceled_at": time.time(),
        "error_code": "analysis_canceled",
        "error_message": CANCELED_MESSAGE,
    }
    if submission_id:
        updates["submission_id"] = str(submission_id)
    if upload_id:
        updates["upload_id"] = str(upload_id)
    _update_job(job_id, **updates)


async def _read_upload_file(file: UploadFile) -> bytes:
    max_bytes = _max_file_bytes()
    chunks: list[bytes] = []
    total_bytes = 0

    while True:
        chunk = await file.read(READ_CHUNK_SIZE)
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise ValueError("file_too_large")
        chunks.append(chunk)

    if total_bytes == 0:
        raise ValueError("empty_file")

    return b"".join(chunks)


def _rate_limit_allows(client_ip: str) -> bool:
    now = time.time()
    window_seconds = _env_int(
        "ANALYZER_API_RATE_LIMIT_WINDOW_SECONDS",
        DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
    )
    max_hits = _env_int("ANALYZER_API_RATE_LIMIT_MAX", DEFAULT_RATE_LIMIT_MAX)
    cutoff = now - window_seconds

    with rate_limit_lock:
        hits = [hit for hit in rate_limit_hits.get(client_ip, []) if hit >= cutoff]
        if len(hits) >= max_hits:
            rate_limit_hits[client_ip] = hits
            return False
        hits.append(now)
        rate_limit_hits[client_ip] = hits
        return True


def _prefill_rate_limit_allows(client_ip: str) -> bool:
    now = time.time()
    window_seconds = _env_int(
        "ANALYZER_API_PREFILL_RATE_LIMIT_WINDOW_SECONDS",
        DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
    )
    max_hits = _env_int("ANALYZER_API_PREFILL_RATE_LIMIT_MAX", DEFAULT_PREFILL_RATE_LIMIT_MAX)
    cutoff = now - window_seconds

    with prefill_rate_limit_lock:
        hits = [hit for hit in prefill_rate_limit_hits.get(client_ip, []) if hit >= cutoff]
        if len(hits) >= max_hits:
            prefill_rate_limit_hits[client_ip] = hits
            return False
        hits.append(now)
        prefill_rate_limit_hits[client_ip] = hits
        return True


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _max_file_bytes() -> int:
    max_mb = _env_int("ANALYZER_API_MAX_FILE_MB", DEFAULT_MAX_FILE_MB)
    return max(max_mb, 1) * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        logger.warning("Invalid integer env var %s; using default.", name)
        return default


def _file_extension(filename: str) -> str:
    dot_index = filename.rfind(".")
    if dot_index == -1:
        return ""
    return filename[dot_index:].lower()


def _is_valid_cid(cid: str) -> bool:
    return bool(cid and CID_PATTERN.match(cid))


def _fetch_ghl_contact(cid: str) -> tuple[Dict[str, Any], Optional[str]]:
    base_url = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com").rstrip("/")
    token = os.getenv("GHL_BEARER_TOKEN", "")
    version = os.getenv("GHL_API_VERSION", "2021-07-28")
    if not base_url or not token:
        logger.warning("[ghl_prefill] missing GHL config.")
        return {}, "not_configured"

    url = f"{base_url}/contacts/{cid}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Version": version,
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
    except requests.RequestException:
        logger.warning("[ghl_prefill] request failed cid=%s", cid)
        return {}, "not_found"
    if response.status_code != 200:
        logger.warning("[ghl_prefill] lookup failed cid=%s status=%s", cid, response.status_code)
        return {}, "not_found"
    try:
        payload = response.json()
    except ValueError:
        logger.warning("[ghl_prefill] invalid json cid=%s", cid)
        return {}, "not_found"
    if isinstance(payload, dict) and isinstance(payload.get("contact"), dict):
        return payload["contact"], None
    if isinstance(payload, dict):
        return payload, None
    return {}, "not_found"


def _contact_matches_location(contact: Dict[str, Any]) -> bool:
    location_id = os.getenv("LOCATION_ID", "").strip()
    contact_location = contact.get("locationId") or contact.get("location_id")
    if location_id and contact_location and str(contact_location) != location_id:
        return False
    return True


def _extract_office_name(contact: Dict[str, Any]) -> str:
    field_id = os.getenv("GHL_OFFICE_FIELD_ID", "").strip()
    if not field_id:
        return ""
    custom_fields = contact.get("customFields") or contact.get("custom_fields") or []
    if not isinstance(custom_fields, list):
        return ""
    for field in custom_fields:
        if not isinstance(field, dict):
            continue
        if str(field.get("id")) == field_id:
            return _clean_contact_text(field.get("value"))
    return ""


def _extract_phone(contact: Dict[str, Any]) -> str:
    for key in ("phone", "phoneNumber", "phone_number"):
        phone = _clean_contact_text(contact.get(key))
        if phone:
            return phone
    return ""


def _safe_prefill_response(cid: str, contact: Dict[str, Any]) -> Dict[str, Any]:
    fields = {
        "firstName": _clean_contact_text(contact.get("firstName") or contact.get("first_name")),
        "lastName": _clean_contact_text(contact.get("lastName") or contact.get("last_name")),
        "email": _clean_contact_text(contact.get("email")),
        "officeName": _extract_office_name(contact),
        "phone": _extract_phone(contact),
    }
    locked_fields = [field_name for field_name, value in fields.items() if value]
    return {
        "ok": True,
        "cid": cid,
        "firstName": fields["firstName"],
        "lastName": fields["lastName"],
        "email": fields["email"],
        "officeName": fields["officeName"],
        "phone": fields["phone"],
        "lockedFields": locked_fields,
    }


def _clean_contact_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, dict):
        return ""
    text = str(value).strip()
    return text[:255]


def _error_message_for_code(code: str) -> str:
    if code == "file_too_large":
        return "Uploaded file is too large."
    if code == "empty_file":
        return "Uploaded file is empty."
    return "Analyzer submission could not be accepted."


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
