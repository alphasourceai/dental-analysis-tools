from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Optional

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
READ_CHUNK_SIZE = 1024 * 1024

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


@app.get("/")
def root() -> Dict[str, Any]:
    return {"ok": True, "service": "public-analyzer-api"}


@app.head("/")
def root_head() -> Response:
    return Response(status_code=200)


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "service": "public-analyzer-api"}


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
    _log_job_status("started", job_id)
    _update_job(job_id, status="processing")
    try:
        from public_analyzer_service import submit_public_analyzer_submission

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
        )

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
    except Exception:
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
            "submission_id": None,
            "upload_id": None,
            "error_code": None,
            "error_message": None,
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
