from __future__ import annotations

import calendar
import csv
import io
import logging
import json
import mimetypes
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, unquote, urlparse
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import requests
import stripe
from fastapi import Body, FastAPI, File, Form, Query, Request, Response, UploadFile as FastAPIUploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_, func, or_

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
from consulting_agreements import (
    AGREEMENT_DOCUMENT_TYPE,
    AGREEMENT_TEMPLATE_PATH,
    AGREEMENT_TEMPLATE_VERSION,
    AGREEMENTS_EMAIL_LINK_TTL_SECONDS,
    AGREEMENTS_SIGNED_URL_TTL_SECONDS,
    AgreementServiceError,
    agreement_email_configured,
    build_signed_agreement_pdf,
    build_signing_url,
    build_template_snapshot,
    create_agreement_signed_url,
    download_agreement_file,
    generate_signer_token,
    hash_signer_token,
    normalize_admin_agreement_payload,
    normalize_email as normalize_agreement_email,
    parse_signature_image,
    payload_template_values,
    render_agreement_pdf,
    send_agreement_ba_countersign_request_email,
    send_agreement_signature_request_email,
    send_agreement_signed_copy_email,
    upload_agreement_file,
    utcnow as agreement_utcnow,
)
from database import SessionLocal
from models import (
    AdminAnalysisJob,
    AdminAnalysisJobFile,
    AdminAnalysisPhiAcknowledgment,
    AdminAuditEvent,
    AdminUser,
    BillingOverride,
    ClientSubmission,
    ConsultingAgreement,
    PublicAnalyticsEvent,
    PublicLeadDraft,
    StripeCheckoutSession,
    StripeCheckoutSessionUpload,
    StripeCustomer,
    StripeEvent,
    StripeSubscription,
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
from upload_portal import (
    PortalError,
    complete_upload,
    create_signed_upload_url,
    create_upload_request,
    verify_upload_token,
)

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="Admin API")
UPLOAD_PORTAL_STATIC_ROOT = Path(__file__).resolve().parent / "upload_portal_static"
UPLOAD_PORTAL_DEFAULT_ALLOWED_ORIGINS = [
    "https://upload.alphasourceai.com",
    "https://alphasourceai.com",
    "https://www.alphasourceai.com",
]
PUBLIC_SITE_DEFAULT_ALLOWED_ORIGINS = [
    "https://alphasourceconsulting.com",
    "https://www.alphasourceconsulting.com",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

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
ADMIN_ANALYSIS_PHI_ACK_VERSION = "admin_document_analysis_phi_ack_v1"
ADMIN_ANALYSIS_PHI_ACK_TEXT = (
    "I confirm this file has been reviewed and is approved/sanitized for AI-assisted analysis, "
    "does not contain unsanitized PHI, and is appropriate to process through the Document Analysis workflow."
)
ADMIN_AUDIT_METADATA_MAX_STRING = 500
ADMIN_AUDIT_METADATA_MAX_BYTES = 12000
ADMIN_AUDIT_EXPORT_MAX_ROWS = 10000
try:
    ADMIN_AUDIT_MOUNTAIN_TZ = ZoneInfo("America/Denver")
except Exception:
    ADMIN_AUDIT_MOUNTAIN_TZ = timezone(timedelta(hours=-7))
ADMIN_AUDIT_METADATA_DENY_KEYS = {
    "apikey",
    "checkouturl",
    "documenttext",
    "extractedtext",
    "filename",
    "gcsobject",
    "gcspath",
    "gspath",
    "objectname",
    "originalfilename",
    "password",
    "secret",
    "signedurl",
    "token",
    "url",
}

PUBLIC_ANALYTICS_RATE_WINDOW_SECONDS = 60
PUBLIC_ANALYTICS_EVENT_RATE_LIMIT = 180
PUBLIC_ANALYTICS_LEAD_RATE_LIMIT = 30
PUBLIC_ANALYTICS_MAX_EVENT_QUERY = 5000
PUBLIC_ANALYTICS_MAX_LEAD_EXPORT_ROWS = 10000
PUBLIC_ANALYTICS_ALLOWED_EVENTS = {
    "page_viewed",
    "cta_clicked",
    "lead_form_viewed",
    "lead_form_started",
    "lead_form_field_completed",
    "lead_form_submit_attempted",
    "lead_form_submit_failed",
    "lead_form_submit_succeeded",
    "lead_form_abandoned",
    "lead_draft_saved",
    "lead_draft_save_failed",
}
PUBLIC_ANALYTICS_EVENT_PROPERTIES = {
    "page_viewed": {"path"},
    "cta_clicked": {"cta_label", "cta_target", "placement"},
    "lead_form_viewed": {"form_id", "form_type", "product_interest"},
    "lead_form_started": {"form_id", "form_type", "product_interest", "first_field"},
    "lead_form_field_completed": {"form_id", "form_type", "product_interest", "field_name"},
    "lead_form_submit_attempted": {"form_id", "form_type", "product_interest"},
    "lead_form_submit_failed": {"form_id", "form_type", "product_interest", "error_type"},
    "lead_form_submit_succeeded": {"form_id", "form_type", "product_interest"},
    "lead_form_abandoned": {"form_id", "form_type", "product_interest", "fields_completed"},
    "lead_draft_saved": {"form_id", "form_type", "product_interest", "status", "fields_completed"},
    "lead_draft_save_failed": {"form_id", "form_type", "product_interest", "status", "error_type"},
}
PUBLIC_ANALYTICS_UTM_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
}
PUBLIC_ANALYTICS_SENSITIVE_KEY_RE = re.compile(
    r"(email|phone|name|message|password|token|secret|auth|authorization|cookie|session|ip|user[_-]?agent)",
    re.IGNORECASE,
)
PUBLIC_ANALYTICS_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,160}$")
_public_analytics_rate_buckets: dict[str, tuple[float, int]] = {}
ADMIN_ONE_TIME_OFFER_PAYMENT_LINKS = {
    "practice_opportunity_review": {
        "name": "Practice Opportunity Review",
        "default_amount": 99500,
    },
    "revenue_leak_sprint": {
        "name": "Revenue Leak Sprint",
        "default_amount": 350000,
    },
    "ar_claims_cleanup_sprint": {
        "name": "AR / Claims Cleanup Sprint",
        "default_amount": 250000,
    },
    "growth_new_patient_conversion_sprint": {
        "name": "Growth + New Patient Conversion Sprint",
        "default_amount": 350000,
    },
}
ADMIN_RECURRING_OFFER_PAYMENT_LINKS = {
    "operations_intelligence_partner": {
        "name": "Operations Intelligence Partner",
        "interval": "month",
    },
}
ADMIN_RETAINER_CONTRACT_MONTHS_MIN = 1
ADMIN_RETAINER_CONTRACT_MONTHS_MAX = 24
PERMISSION_CLIENTS_READ = "clients_read"
PERMISSION_CLIENTS_WRITE = "clients_write"
PERMISSION_UPLOADS_WRITE = "uploads_write"
PERMISSION_BILLING_READ = "billing_read"
PERMISSION_BILLING_WRITE = "billing_write"
PERMISSION_ANALYSIS_READ = "analysis_read"
PERMISSION_ANALYSIS_WRITE = "analysis_write"
PERMISSION_PDF_READ = "pdf_read"
PERMISSION_PDF_GENERATE = "pdf_generate"
PERMISSION_SECURE_UPLOADS_READ = "secure_uploads_read"
PERMISSION_SECURE_UPLOADS_WRITE = "secure_uploads_write"
PERMISSION_AGREEMENTS_READ = "agreements_read"
PERMISSION_AGREEMENTS_WRITE = "agreements_write"
PERMISSION_ADMIN_MANAGEMENT_READ = "admin_management_read"
PERMISSION_ADMIN_MANAGEMENT_WRITE = "admin_management_write"
PERMISSION_AUDIT_READ = "audit_read"
PERMISSION_SITE_ANALYTICS_READ = "site_analytics_read"
PERMISSION_SITE_ANALYTICS_WRITE = "site_analytics_write"
ADMIN_API_DASHBOARD_ROLES = {"super_admin", "admin", "analyst", "billing_admin", "viewer"}
ADMIN_API_ALL_PERMISSIONS = {
    PERMISSION_CLIENTS_READ,
    PERMISSION_CLIENTS_WRITE,
    PERMISSION_UPLOADS_WRITE,
    PERMISSION_BILLING_READ,
    PERMISSION_BILLING_WRITE,
    PERMISSION_ANALYSIS_READ,
    PERMISSION_ANALYSIS_WRITE,
    PERMISSION_PDF_READ,
    PERMISSION_PDF_GENERATE,
    PERMISSION_SECURE_UPLOADS_READ,
    PERMISSION_SECURE_UPLOADS_WRITE,
    PERMISSION_AGREEMENTS_READ,
    PERMISSION_AGREEMENTS_WRITE,
    PERMISSION_ADMIN_MANAGEMENT_READ,
    PERMISSION_ADMIN_MANAGEMENT_WRITE,
    PERMISSION_AUDIT_READ,
    PERMISSION_SITE_ANALYTICS_READ,
    PERMISSION_SITE_ANALYTICS_WRITE,
}
ADMIN_ROLE_PERMISSION_MAP = {
    "super_admin": ADMIN_API_ALL_PERMISSIONS,
    "admin": ADMIN_API_ALL_PERMISSIONS - {PERMISSION_ADMIN_MANAGEMENT_WRITE, PERMISSION_AUDIT_READ},
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

configured_public_site_origins = [
    origin.strip()
    for origin in os.getenv("PUBLIC_SITE_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
allowed_origins = list(
    dict.fromkeys(
        [
            *[
                origin.strip()
                for origin in os.getenv("ADMIN_API_ALLOWED_ORIGINS", "").split(",")
                if origin.strip()
            ],
            *(configured_public_site_origins or PUBLIC_SITE_DEFAULT_ALLOWED_ORIGINS),
        ]
    )
)

if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )


def _upload_portal_allowed_origin(origin: Optional[str]) -> Optional[str]:
    allowlist = [
        item.strip()
        for item in os.getenv("PORTAL_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    ]
    if not allowlist:
        allowlist = UPLOAD_PORTAL_DEFAULT_ALLOWED_ORIGINS
    if origin and origin in allowlist:
        return origin
    return None


def _upload_portal_origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    return _upload_portal_allowed_origin(origin) is not None


def _upload_portal_cors_headers(request: Request) -> dict[str, str]:
    headers = {
        "Access-Control-Allow-Headers": "authorization, content-type",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    }
    allowed_origin = _upload_portal_allowed_origin(request.headers.get("origin"))
    if allowed_origin:
        headers["Access-Control-Allow-Origin"] = allowed_origin
        headers["Vary"] = "Origin"
    return headers


def _upload_portal_json_response(
    request: Request,
    status_code: int,
    payload: dict[str, Any],
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers=_upload_portal_cors_headers(request),
    )


def _upload_portal_error_response(request: Request, exc: PortalError) -> JSONResponse:
    status_code = exc.status if isinstance(exc.status, int) else 400
    return _upload_portal_json_response(
        request,
        status_code,
        {"error": exc.message, "code": exc.code, "detail": exc.detail},
    )


def _public_site_allowed_origin(origin: Optional[str]) -> Optional[str]:
    allowlist = configured_public_site_origins
    if not allowlist:
        allowlist = PUBLIC_SITE_DEFAULT_ALLOWED_ORIGINS
    if origin and origin in allowlist:
        return origin
    return None


def _public_site_origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    return not origin or _public_site_allowed_origin(origin) is not None


def _public_site_cors_headers(request: Request) -> dict[str, str]:
    headers = {
        "Access-Control-Allow-Headers": "content-type",
        "Access-Control-Allow-Methods": "POST,OPTIONS",
        "Cache-Control": "no-store",
    }
    allowed_origin = _public_site_allowed_origin(request.headers.get("origin"))
    if allowed_origin:
        headers["Access-Control-Allow-Origin"] = allowed_origin
        headers["Vary"] = "Origin"
    return headers


def _public_site_json_response(request: Request, status_code: int, payload: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=payload, headers=_public_site_cors_headers(request))


async def _public_site_json_body(request: Request) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    try:
        body = await request.json()
    except Exception:
        return {}, _public_site_json_response(
            request,
            400,
            {"error": {"code": "invalid_json", "message": "Invalid request."}},
        )
    if not isinstance(body, dict):
        return {}, _public_site_json_response(
            request,
            400,
            {"error": {"code": "invalid_json", "message": "Invalid request."}},
        )
    return body, None


def _public_site_rate_limited(request: Request, *, limit: int) -> bool:
    client_key = _request_client_ip(request) or "unknown"
    now = time.monotonic()
    window_start, count = _public_analytics_rate_buckets.get(client_key, (now, 0))
    if now - window_start >= PUBLIC_ANALYTICS_RATE_WINDOW_SECONDS:
        window_start, count = now, 0
    count += 1
    _public_analytics_rate_buckets[client_key] = (window_start, count)
    if len(_public_analytics_rate_buckets) > 5000:
        cutoff = now - PUBLIC_ANALYTICS_RATE_WINDOW_SECONDS
        stale_keys = [key for key, (started_at, _) in _public_analytics_rate_buckets.items() if started_at < cutoff]
        for key in stale_keys:
            _public_analytics_rate_buckets.pop(key, None)
    return count > limit


def _public_site_trim(value: object, max_length: int = 300) -> str:
    return str(value or "").replace("\x00", "").strip()[:max_length]


def _public_site_path(value: object) -> str:
    raw = _public_site_trim(value, 600)
    if not raw:
        return "/"
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return _public_site_trim(parsed.path or "/external", 300)
    path = raw.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        return "/"
    return _public_site_trim(path, 300) or "/"


def _public_site_identifier(value: object) -> str:
    candidate = _public_site_trim(value, 160)
    return candidate if PUBLIC_ANALYTICS_ID_RE.fullmatch(candidate) else ""


def _public_site_utm(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        key_text = _public_site_trim(key, 80).lower()
        if key_text not in PUBLIC_ANALYTICS_UTM_KEYS:
            continue
        item_text = _public_site_trim(item, 160)
        if item_text:
            result[key_text] = item_text
    return result


def _public_site_event_properties(event_name: str, value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    allowed_keys = PUBLIC_ANALYTICS_EVENT_PROPERTIES.get(event_name, set())
    result: dict[str, object] = {}
    for key, item in value.items():
        key_text = _public_site_trim(key, 80).lower()
        if key_text not in allowed_keys or PUBLIC_ANALYTICS_SENSITIVE_KEY_RE.search(key_text):
            continue
        if key_text == "fields_completed":
            if not isinstance(item, list):
                continue
            values = []
            for item_value in item[:20]:
                item_text = _public_site_trim(item_value, 80)
                if item_text and not PUBLIC_ANALYTICS_SENSITIVE_KEY_RE.search(item_text):
                    values.append(item_text)
            result[key_text] = values
            continue
        if isinstance(item, bool):
            result[key_text] = item
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            result[key_text] = item
        else:
            item_text = _public_site_trim(item, 180)
            if item_text:
                result[key_text] = item_text
    return result


def _public_site_email(value: object) -> str:
    email = _public_site_trim(value, 254).lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return ""
    return email


def _public_site_lead_fields(value: object, *, status: str) -> tuple[dict[str, str], list[str], Optional[str]]:
    if not isinstance(value, dict):
        return {}, [], "Contact details are required."
    fields = {
        "first_name": _public_site_trim(value.get("first_name"), 100),
        "last_name": _public_site_trim(value.get("last_name"), 100),
        "email": _public_site_email(value.get("email")),
        "phone": _public_site_trim(value.get("phone"), 40),
        "message": _public_site_trim(value.get("message"), 2000) if status == "submitted" else "",
    }
    phone_digits = re.sub(r"\D+", "", fields["phone"])
    if status == "submitted" and (not fields["first_name"] or not fields["last_name"] or not fields["email"]):
        return {}, [], "First name, last name, and a valid email address are required."
    if status != "submitted" and not fields["email"] and len(phone_digits) < 7:
        return {}, [], "An email address or phone number is required."
    return fields, [name for name, item in fields.items() if item], None


def _site_analytics_date_range(
    start_date: Optional[str],
    end_date: Optional[str],
) -> tuple[datetime, datetime, Optional[JSONResponse]]:
    today = datetime.now(timezone.utc).date()
    try:
        start_value = date.fromisoformat(start_date) if start_date else today - timedelta(days=29)
        end_value = date.fromisoformat(end_date) if end_date else today
    except ValueError:
        return datetime.now(timezone.utc), datetime.now(timezone.utc), _error_response(
            400,
            "invalid_date_range",
            "Dates must use YYYY-MM-DD.",
        )
    if start_value > end_value or (end_value - start_value).days > 366:
        return datetime.now(timezone.utc), datetime.now(timezone.utc), _error_response(
            400,
            "invalid_date_range",
            "Select a date range of up to 366 days.",
        )
    return (
        datetime.combine(start_value, datetime.min.time(), tzinfo=timezone.utc),
        datetime.combine(end_value + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc),
        None,
    )


async def _upload_portal_json_body(request: Request) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    try:
        body = await request.json()
    except Exception:
        return {}, _upload_portal_json_response(
            request,
            400,
            {"error": "Invalid JSON payload", "code": "invalid_json", "detail": None},
        )
    if not isinstance(body, dict):
        return {}, _upload_portal_json_response(
            request,
            400,
            {"error": "Invalid JSON payload", "code": "invalid_json", "detail": None},
        )
    return body, None


def _upload_portal_bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    return ""


def _upload_portal_static_path(path: str = "") -> Optional[Path]:
    root = UPLOAD_PORTAL_STATIC_ROOT.resolve()
    if not path:
        file_path = root / "index.html"
    else:
        file_path = (root / unquote(path).lstrip("/")).resolve()
    try:
        file_path.relative_to(root)
    except ValueError:
        return None
    if not file_path.exists() or not file_path.is_file():
        return None
    return file_path


def _upload_portal_static_response(path: str = "") -> Response:
    file_path = _upload_portal_static_path(path)
    if not file_path:
        return Response("Not found", status_code=404, media_type="text/plain")
    media_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    return FileResponse(str(file_path), media_type=media_type)


@app.get("/")
def root() -> dict[str, object]:
    return {"ok": True, "service": "admin-api"}


@app.head("/")
def root_head() -> Response:
    return Response(status_code=200)


@app.get("/health")
def health() -> dict[str, object]:
    return {"ok": True, "service": "admin-api"}


@app.get("/uploads")
def upload_portal_index() -> Response:
    return _upload_portal_static_response()


@app.get("/uploads/")
def upload_portal_index_slash() -> Response:
    return _upload_portal_static_response()


@app.get("/uploads/{path:path}")
def upload_portal_static(path: str) -> Response:
    return _upload_portal_static_response(path)


@app.options("/api/upload-portal/{path:path}")
def upload_portal_options(path: str, request: Request) -> Response:
    if not _upload_portal_origin_allowed(request):
        return _upload_portal_json_response(
            request,
            403,
            {"error": "Origin not allowed", "code": "forbidden", "detail": None},
        )
    return Response(status_code=204, headers=_upload_portal_cors_headers(request))


@app.get("/api/upload-portal/health")
def upload_portal_health(request: Request) -> JSONResponse:
    return _upload_portal_json_response(request, 200, {"ok": True, "status": "healthy"})


@app.post("/api/upload-portal/verify")
async def upload_portal_verify(request: Request) -> JSONResponse:
    if not _upload_portal_origin_allowed(request):
        return _upload_portal_json_response(
            request,
            403,
            {"error": "Origin not allowed", "code": "forbidden", "detail": None},
        )
    body, parse_error = await _upload_portal_json_body(request)
    if parse_error:
        return parse_error
    try:
        result = verify_upload_token(body.get("token", ""))
    except PortalError as exc:
        return _upload_portal_error_response(request, exc)
    except Exception:
        logger.exception("[upload_portal] verify failed")
        return _upload_portal_json_response(
            request,
            500,
            {"error": "Server error", "code": "server_error", "detail": None},
        )
    return _upload_portal_json_response(request, 200, {"ok": True, "data": result})


@app.post("/api/upload-portal/signed-upload-url")
async def upload_portal_signed_upload_url(request: Request) -> JSONResponse:
    if not _upload_portal_origin_allowed(request):
        return _upload_portal_json_response(
            request,
            403,
            {"error": "Origin not allowed", "code": "forbidden", "detail": None},
        )
    body, parse_error = await _upload_portal_json_body(request)
    if parse_error:
        return parse_error
    try:
        result = create_signed_upload_url(
            _upload_portal_bearer_token(request),
            body.get("filename", ""),
            body.get("content_type"),
            body.get("byte_size"),
        )
    except PortalError as exc:
        return _upload_portal_error_response(request, exc)
    except Exception:
        logger.exception("[upload_portal] signed upload URL creation failed")
        return _upload_portal_json_response(
            request,
            500,
            {"error": "Server error", "code": "server_error", "detail": None},
        )
    return _upload_portal_json_response(request, 200, {"ok": True, "data": result})


@app.post("/api/upload-portal/complete")
async def upload_portal_complete(request: Request) -> JSONResponse:
    if not _upload_portal_origin_allowed(request):
        return _upload_portal_json_response(
            request,
            403,
            {"error": "Origin not allowed", "code": "forbidden", "detail": None},
        )
    body, parse_error = await _upload_portal_json_body(request)
    if parse_error:
        return parse_error
    try:
        result = complete_upload(
            _upload_portal_bearer_token(request),
            body.get("upload_id", ""),
            request_ip=_request_client_ip(request),
            user_agent=_request_user_agent(request),
        )
    except PortalError as exc:
        return _upload_portal_error_response(request, exc)
    except Exception:
        logger.exception("[upload_portal] upload completion failed")
        return _upload_portal_json_response(
            request,
            500,
            {"error": "Server error", "code": "server_error", "detail": None},
        )
    return _upload_portal_json_response(request, 200, {"ok": True, "data": result})


@app.options("/api/public-analytics/{path:path}")
async def public_analytics_options(request: Request, path: str) -> Response:
    del path
    if not _public_site_origin_allowed(request):
        return Response(status_code=403, headers=_public_site_cors_headers(request))
    return Response(status_code=204, headers=_public_site_cors_headers(request))


@app.options("/api/public-leads/{path:path}")
async def public_leads_options(request: Request, path: str) -> Response:
    del path
    if not _public_site_origin_allowed(request):
        return Response(status_code=403, headers=_public_site_cors_headers(request))
    return Response(status_code=204, headers=_public_site_cors_headers(request))


@app.post("/api/public-analytics/events")
async def record_public_analytics_event(request: Request) -> JSONResponse:
    if not _public_site_origin_allowed(request):
        return _public_site_json_response(
            request,
            403,
            {"error": {"code": "forbidden", "message": "Request not accepted."}},
        )
    if _public_site_rate_limited(request, limit=PUBLIC_ANALYTICS_EVENT_RATE_LIMIT):
        return _public_site_json_response(
            request,
            429,
            {"error": {"code": "rate_limited", "message": "Request not accepted."}},
        )

    body, parse_error = await _public_site_json_body(request)
    if parse_error:
        return parse_error
    event_name = _public_site_trim(body.get("event_name"), 81).lower()
    if event_name not in PUBLIC_ANALYTICS_ALLOWED_EVENTS:
        return _public_site_json_response(
            request,
            400,
            {"error": {"code": "invalid_event", "message": "Request not accepted."}},
        )

    db = SessionLocal()
    try:
        db.add(
            PublicAnalyticsEvent(
                event_name=event_name,
                anonymous_id=_public_site_identifier(body.get("anonymous_id")) or None,
                session_id=_public_site_identifier(body.get("session_id")) or None,
                path=_public_site_path(body.get("path")),
                page_title=_public_site_trim(body.get("page_title"), 180) or None,
                referrer_path=_public_site_path(body.get("referrer_path")),
                utm=_public_site_utm(body.get("utm")),
                properties=_public_site_event_properties(event_name, body.get("properties")),
                occurred_at=datetime.now(timezone.utc),
                request_id=_public_site_trim(request.headers.get("x-request-id"), 100) or None,
            )
        )
        db.commit()
        return _public_site_json_response(request, 201, {"ok": True})
    except Exception:
        db.rollback()
        logger.warning("[public_analytics] event storage failed event_name=%s", event_name)
        return _public_site_json_response(
            request,
            503,
            {"error": {"code": "event_unavailable", "message": "Request not accepted."}},
        )
    finally:
        db.close()


@app.post("/api/public-leads/draft")
async def save_public_lead_draft(request: Request) -> JSONResponse:
    if not _public_site_origin_allowed(request):
        return _public_site_json_response(
            request,
            403,
            {"error": {"code": "forbidden", "message": "Request not accepted."}},
        )
    if _public_site_rate_limited(request, limit=PUBLIC_ANALYTICS_LEAD_RATE_LIMIT):
        return _public_site_json_response(
            request,
            429,
            {"error": {"code": "rate_limited", "message": "Please wait a moment and try again."}},
        )

    body, parse_error = await _public_site_json_body(request)
    if parse_error:
        return parse_error
    try:
        draft_id = UUID(str(body.get("draft_id") or ""))
    except (TypeError, ValueError):
        return _public_site_json_response(
            request,
            400,
            {"error": {"code": "invalid_draft", "message": "Request not accepted."}},
        )
    status = _public_site_trim(body.get("status"), 20).lower()
    if status not in {"partial", "abandoned", "submitted"}:
        return _public_site_json_response(
            request,
            400,
            {"error": {"code": "invalid_status", "message": "Request not accepted."}},
        )
    fields, default_completed, field_error = _public_site_lead_fields(body.get("fields"), status=status)
    if field_error:
        return _public_site_json_response(
            request,
            400,
            {"error": {"code": "invalid_contact", "message": field_error}},
        )
    source = body.get("source") if isinstance(body.get("source"), dict) else {}
    provided_fields = body.get("fields_completed") if isinstance(body.get("fields_completed"), list) else default_completed
    allowed_completed = {"first_name", "last_name", "email", "phone", "message"}
    fields_completed = [
        _public_site_trim(item, 40)
        for item in provided_fields[:20]
        if _public_site_trim(item, 40) in allowed_completed
    ]
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        lead = db.query(PublicLeadDraft).filter(PublicLeadDraft.id == draft_id).first()
        if lead is None:
            lead = PublicLeadDraft(
                id=draft_id,
                status=status,
                created_at=now,
                expires_at=now + timedelta(days=90),
            )
            db.add(lead)
        elif str(getattr(lead, "status", "")) == "submitted":
            status = "submitted"

        lead.status = status
        lead.form_id = _public_site_trim(body.get("form_id"), 100) or None
        lead.form_type = _public_site_trim(body.get("form_type"), 80) or None
        lead.product_interest = _public_site_trim(body.get("product_interest"), 160) or None
        lead.first_name = fields["first_name"] or None
        lead.last_name = fields["last_name"] or None
        lead.email = fields["email"] or None
        lead.phone = fields["phone"] or None
        if status == "submitted":
            lead.message = fields["message"] or None
            lead.submitted_at = getattr(lead, "submitted_at", None) or now
        lead.fields_completed = list(dict.fromkeys(fields_completed))
        lead.last_field = _public_site_trim(body.get("last_field"), 40) or None
        lead.source_path = _public_site_path(source.get("path"))
        lead.source_referrer_path = _public_site_path(source.get("referrer_path"))
        lead.source_cta = _public_site_trim(source.get("cta"), 160) or None
        lead.utm = _public_site_utm(source.get("utm"))
        lead.anonymous_id = _public_site_identifier(body.get("anonymous_id")) or None
        lead.session_id = _public_site_identifier(body.get("session_id")) or None
        lead.privacy_notice_version = _public_site_trim(body.get("privacy_notice_version"), 100) or None
        lead.request_id = _public_site_trim(request.headers.get("x-request-id"), 100) or None
        lead.updated_at = now
        lead.expires_at = now + timedelta(days=90)
        db.commit()
        return _public_site_json_response(
            request,
            200,
            {"ok": True, "lead": {"id": str(lead.id), "status": lead.status}},
        )
    except Exception:
        db.rollback()
        logger.warning("[public_leads] lead storage failed status=%s", status)
        return _public_site_json_response(
            request,
            503,
            {"error": {"code": "lead_unavailable", "message": "We could not save your request. Please try again."}},
        )
    finally:
        db.close()


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


@app.get("/api/admin/audit-events")
def list_admin_audit_events(
    request: Request,
    startDate: Optional[str] = None,
    endDate: Optional[str] = None,
    eventType: Optional[str] = None,
    clientEmail: Optional[str] = None,
    actorEmail: Optional[str] = None,
    targetType: Optional[str] = None,
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_AUDIT_READ)
    if error_response:
        return error_response

    start_dt, end_dt, validation_error = _audit_date_range(startDate, endDate)
    if validation_error:
        return validation_error

    safe_limit = min(limit, 100)
    db = SessionLocal()
    try:
        query = _audit_events_filtered_query(
            db,
            start_dt=start_dt,
            end_dt=end_dt,
            event_type=eventType,
            client_email=clientEmail,
            actor_email=actorEmail,
            target_type=targetType,
        )
        rows = (
            query.order_by(AdminAuditEvent.occurred_at.desc(), AdminAuditEvent.created_at.desc())
            .offset(offset)
            .limit(safe_limit + 1)
            .all()
        )
        has_more = len(rows) > safe_limit
        rows = rows[:safe_limit]
        return JSONResponse(
            {
                "ok": True,
                "items": [_audit_event_payload(row) for row in rows],
                "count": len(rows),
                "hasMore": has_more,
            }
        )
    except Exception:
        logger.exception("[admin_audit] audit event lookup failed.")
        return _error_response(500, "audit_events_lookup_failed", "Unable to load audit events.")
    finally:
        db.close()


@app.get("/api/admin/audit-events/export.csv")
def export_admin_audit_events_csv(
    request: Request,
    startDate: Optional[str] = None,
    endDate: Optional[str] = None,
    eventType: Optional[str] = None,
    clientEmail: Optional[str] = None,
    actorEmail: Optional[str] = None,
    targetType: Optional[str] = None,
) -> Response:
    _, _, error_response = _require_admin_permission(request, PERMISSION_AUDIT_READ)
    if error_response:
        return error_response

    start_dt, end_dt, validation_error = _audit_date_range(startDate, endDate)
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        rows = (
            _audit_events_filtered_query(
                db,
                start_dt=start_dt,
                end_dt=end_dt,
                event_type=eventType,
                client_email=clientEmail,
                actor_email=actorEmail,
                target_type=targetType,
            )
            .order_by(AdminAuditEvent.occurred_at.desc(), AdminAuditEvent.created_at.desc())
            .limit(ADMIN_AUDIT_EXPORT_MAX_ROWS)
            .all()
        )
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "occurred_at_utc",
                "occurred_at_mst",
                "source",
                "event_type",
                "actor_email",
                "actor_role",
                "client_email",
                "target_type",
                "target_id",
                "ip_address",
                "device_summary",
                "location",
                "metadata_json",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    _audit_csv_cell(_iso_datetime(getattr(row, "occurred_at", None))),
                    _audit_csv_cell(_audit_mountain_datetime(getattr(row, "occurred_at", None))),
                    _audit_csv_cell(getattr(row, "source", None)),
                    _audit_csv_cell(getattr(row, "event_type", None)),
                    _audit_csv_cell(getattr(row, "actor_admin_email", None)),
                    _audit_csv_cell(getattr(row, "actor_role", None)),
                    _audit_csv_cell(getattr(row, "client_email", None)),
                    _audit_csv_cell(getattr(row, "target_type", None)),
                    _audit_csv_cell(getattr(row, "target_id", None)),
                    _audit_csv_cell(getattr(row, "ip_address", None)),
                    _audit_csv_cell(getattr(row, "device_summary", None)),
                    _audit_csv_cell(getattr(row, "location", None)),
                    _audit_csv_cell(_audit_metadata_json(row)),
                ]
            )
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="admin-audit-events.csv"'},
        )
    except Exception:
        logger.exception("[admin_audit] audit event CSV export failed.")
        return _error_response(500, "audit_events_export_failed", "Unable to export audit events.")
    finally:
        db.close()


def _site_analytics_filtered_events_query(
    db: Any,
    *,
    start_at: datetime,
    end_at: datetime,
    path: Optional[str],
    event_name: Optional[str],
) -> Any:
    query = db.query(PublicAnalyticsEvent).filter(PublicAnalyticsEvent.occurred_at >= start_at).filter(
        PublicAnalyticsEvent.occurred_at < end_at
    )
    normalized_path = _public_site_trim(path, 300)
    if normalized_path:
        query = query.filter(PublicAnalyticsEvent.path == _public_site_path(normalized_path))
    normalized_event = _public_site_trim(event_name, 81).lower()
    if normalized_event:
        query = query.filter(PublicAnalyticsEvent.event_name == normalized_event)
    return query


def _site_analytics_filtered_leads_query(
    db: Any,
    *,
    start_at: datetime,
    end_at: datetime,
    status: Optional[str],
    archive: str,
    path: Optional[str],
) -> Any:
    query = db.query(PublicLeadDraft).filter(PublicLeadDraft.updated_at >= start_at).filter(
        PublicLeadDraft.updated_at < end_at
    )
    normalized_status = _public_site_trim(status, 20).lower()
    if normalized_status in {"partial", "abandoned", "submitted"}:
        query = query.filter(PublicLeadDraft.status == normalized_status)
    if archive == "active":
        query = query.filter(PublicLeadDraft.archived_at.is_(None))
    elif archive == "archived":
        query = query.filter(PublicLeadDraft.archived_at.is_not(None))
    normalized_path = _public_site_trim(path, 300)
    if normalized_path:
        query = query.filter(PublicLeadDraft.source_path == _public_site_path(normalized_path))
    return query


def _site_analytics_event_payload(row: PublicAnalyticsEvent) -> dict[str, object]:
    properties = getattr(row, "properties", None)
    utm = getattr(row, "utm", None)
    return {
        "id": _id_text(getattr(row, "id", None)),
        "eventName": _clean_text(getattr(row, "event_name", None)),
        "path": _clean_text(getattr(row, "path", None)) or "/",
        "properties": properties if isinstance(properties, dict) else {},
        "utm": utm if isinstance(utm, dict) else {},
        "occurredAt": _iso_datetime(getattr(row, "occurred_at", None)),
    }


def _site_analytics_lead_payload(row: PublicLeadDraft) -> dict[str, object]:
    first_name = _clean_text(getattr(row, "first_name", None))
    last_name = _clean_text(getattr(row, "last_name", None))
    full_name = " ".join(part for part in (first_name, last_name) if part)
    fields_completed = getattr(row, "fields_completed", None)
    utm = getattr(row, "utm", None)
    return {
        "id": _id_text(getattr(row, "id", None)),
        "status": _clean_text(getattr(row, "status", None)),
        "formId": _clean_text(getattr(row, "form_id", None)),
        "formType": _clean_text(getattr(row, "form_type", None)),
        "productInterest": _clean_text(getattr(row, "product_interest", None)),
        "contact": {
            "fullName": full_name or None,
            "email": _clean_text(getattr(row, "email", None)) or None,
            "phone": _clean_text(getattr(row, "phone", None)) or None,
        },
        "messagePreview": _public_site_trim(getattr(row, "message", None), 600) or None,
        "fieldsCompleted": fields_completed if isinstance(fields_completed, list) else [],
        "lastField": _clean_text(getattr(row, "last_field", None)) or None,
        "source": {
            "path": _clean_text(getattr(row, "source_path", None)) or "/",
            "cta": _clean_text(getattr(row, "source_cta", None)) or None,
            "utm": utm if isinstance(utm, dict) else {},
        },
        "submittedAt": _iso_datetime(getattr(row, "submitted_at", None)),
        "updatedAt": _iso_datetime(getattr(row, "updated_at", None)),
        "expiresAt": _iso_datetime(getattr(row, "expires_at", None)),
        "archived": getattr(row, "archived_at", None) is not None,
        "archivedAt": _iso_datetime(getattr(row, "archived_at", None)),
    }


@app.get("/api/admin/site-analytics")
def get_site_analytics(
    request: Request,
    startDate: Optional[str] = None,
    endDate: Optional[str] = None,
    leadStatus: Optional[str] = None,
    archive: str = "active",
    path: Optional[str] = None,
    eventName: Optional[str] = None,
    leadLimit: int = Query(50, ge=1),
    leadOffset: int = Query(0, ge=0),
) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_SITE_ANALYTICS_READ)
    if error_response:
        return error_response
    start_at, end_at, validation_error = _site_analytics_date_range(startDate, endDate)
    if validation_error:
        return validation_error
    normalized_archive = _public_site_trim(archive, 20).lower() or "active"
    if normalized_archive not in {"active", "archived", "all"}:
        return _error_response(400, "invalid_archive_filter", "archive must be active, archived, or all.")
    normalized_lead_status = _public_site_trim(leadStatus, 20).lower()
    if normalized_lead_status and normalized_lead_status not in {"partial", "abandoned", "submitted"}:
        return _error_response(400, "invalid_lead_status", "Unsupported lead status.")
    normalized_event_name = _public_site_trim(eventName, 81).lower()
    if normalized_event_name and normalized_event_name not in PUBLIC_ANALYTICS_ALLOWED_EVENTS:
        return _error_response(400, "invalid_event_name", "Unsupported event name.")

    db = SessionLocal()
    try:
        event_query = _site_analytics_filtered_events_query(
            db,
            start_at=start_at,
            end_at=end_at,
            path=path,
            event_name=normalized_event_name or None,
        )
        lead_query = _site_analytics_filtered_leads_query(
            db,
            start_at=start_at,
            end_at=end_at,
            status=normalized_lead_status or None,
            archive=normalized_archive,
            path=path,
        )
        safe_lead_limit = min(leadLimit, 100)
        lead_rows = lead_query.order_by(PublicLeadDraft.updated_at.desc()).offset(leadOffset).limit(safe_lead_limit + 1).all()
        leads_has_more = len(lead_rows) > safe_lead_limit
        lead_rows = lead_rows[:safe_lead_limit]

        event_rows = event_query.order_by(PublicAnalyticsEvent.occurred_at.desc()).limit(PUBLIC_ANALYTICS_MAX_EVENT_QUERY + 1).all()
        events_sampled = len(event_rows) > PUBLIC_ANALYTICS_MAX_EVENT_QUERY
        event_rows = event_rows[:PUBLIC_ANALYTICS_MAX_EVENT_QUERY]
        all_lead_rows = lead_query.order_by(PublicLeadDraft.updated_at.desc()).limit(PUBLIC_ANALYTICS_MAX_EVENT_QUERY + 1).all()
        leads_sampled = len(all_lead_rows) > PUBLIC_ANALYTICS_MAX_EVENT_QUERY
        all_lead_rows = all_lead_rows[:PUBLIC_ANALYTICS_MAX_EVENT_QUERY]

        page_activity: dict[str, dict[str, object]] = {}
        cta_activity: dict[tuple[str, str, str], int] = {}
        form_activity: dict[tuple[str, str, str], dict[str, int]] = {}
        event_counts: dict[str, int] = {}
        page_views = 0
        cta_clicks = 0
        for row in event_rows:
            event = _clean_text(getattr(row, "event_name", None)) or "unknown"
            event_counts[event] = event_counts.get(event, 0) + 1
            row_path = _clean_text(getattr(row, "path", None)) or "/"
            page = page_activity.setdefault(row_path, {"path": row_path, "pageViews": 0, "ctaClicks": 0, "formActivity": 0, "leadCount": 0})
            properties = getattr(row, "properties", None)
            properties = properties if isinstance(properties, dict) else {}
            if event == "page_viewed":
                page["pageViews"] = int(page["pageViews"]) + 1
                page_views += 1
            if event == "cta_clicked":
                page["ctaClicks"] = int(page["ctaClicks"]) + 1
                cta_clicks += 1
                cta_key = (
                    _public_site_trim(properties.get("cta_label"), 180) or "Unknown CTA",
                    _public_site_trim(properties.get("placement"), 120) or "Unknown placement",
                    _public_site_trim(properties.get("cta_target"), 300) or "Unknown target",
                )
                cta_activity[cta_key] = cta_activity.get(cta_key, 0) + 1
            if event.startswith("lead_"):
                page["formActivity"] = int(page["formActivity"]) + 1
                form_key = (
                    _public_site_trim(properties.get("form_id"), 100) or "contact",
                    _public_site_trim(properties.get("form_type"), 80) or "contact",
                    _public_site_trim(properties.get("product_interest"), 160) or "Dental consulting",
                )
                form_metrics = form_activity.setdefault(form_key, {"viewed": 0, "started": 0, "submitted": 0, "draftSaved": 0, "abandoned": 0})
                if event == "lead_form_viewed":
                    form_metrics["viewed"] += 1
                elif event == "lead_form_started":
                    form_metrics["started"] += 1
                elif event == "lead_form_submit_succeeded":
                    form_metrics["submitted"] += 1
                elif event == "lead_draft_saved":
                    form_metrics["draftSaved"] += 1
                elif event == "lead_form_abandoned":
                    form_metrics["abandoned"] += 1

        lead_status_counts = {"partial": 0, "abandoned": 0, "submitted": 0}
        for lead in all_lead_rows:
            status = _clean_text(getattr(lead, "status", None)).lower()
            if status in lead_status_counts:
                lead_status_counts[status] += 1
            row_path = _clean_text(getattr(lead, "source_path", None)) or "/"
            page = page_activity.setdefault(row_path, {"path": row_path, "pageViews": 0, "ctaClicks": 0, "formActivity": 0, "leadCount": 0})
            page["leadCount"] = int(page["leadCount"]) + 1

        return JSONResponse(
            {
                "ok": True,
                "generatedAt": _iso_datetime(datetime.now(timezone.utc)),
                "dateRange": {"startDate": start_at.date().isoformat(), "endDate": (end_at - timedelta(days=1)).date().isoformat()},
                "sampled": events_sampled or leads_sampled,
                "summary": {
                    "publicAnalyticsEvents": len(event_rows),
                    "pageViews": page_views,
                    "ctaClicks": cta_clicks,
                    "leadCaptures": len(all_lead_rows),
                    "submittedLeads": lead_status_counts["submitted"],
                    "partialLeads": lead_status_counts["partial"],
                    "abandonedLeads": lead_status_counts["abandoned"],
                },
                "leads": {
                    "items": [_site_analytics_lead_payload(row) for row in lead_rows],
                    "count": len(lead_rows),
                    "hasMore": leads_has_more,
                    "offset": leadOffset,
                },
                "pageActivity": sorted(page_activity.values(), key=lambda item: int(item["pageViews"]) + int(item["leadCount"]), reverse=True)[:20],
                "ctaActivity": [
                    {"label": key[0], "placement": key[1], "target": key[2], "count": count}
                    for key, count in sorted(cta_activity.items(), key=lambda item: item[1], reverse=True)[:20]
                ],
                "formActivity": [
                    {"formId": key[0], "formType": key[1], "productInterest": key[2], **metrics}
                    for key, metrics in sorted(form_activity.items(), key=lambda item: item[1]["submitted"], reverse=True)[:20]
                ],
                "eventTypes": [
                    {"eventName": event_name, "count": count}
                    for event_name, count in sorted(event_counts.items(), key=lambda item: item[1], reverse=True)
                ],
                "events": {"items": [_site_analytics_event_payload(row) for row in event_rows[:100]], "count": min(len(event_rows), 100)},
            }
        )
    except Exception:
        logger.exception("[site_analytics] dashboard lookup failed.")
        return _error_response(500, "site_analytics_lookup_failed", "Unable to load site analytics.")
    finally:
        db.close()


@app.get("/api/admin/site-analytics/leads.csv")
def export_site_analytics_leads_csv(
    request: Request,
    startDate: Optional[str] = None,
    endDate: Optional[str] = None,
    leadStatus: Optional[str] = None,
    archive: str = "active",
    path: Optional[str] = None,
) -> Response:
    _, _, error_response = _require_admin_permission(request, PERMISSION_SITE_ANALYTICS_READ)
    if error_response:
        return error_response
    start_at, end_at, validation_error = _site_analytics_date_range(startDate, endDate)
    if validation_error:
        return validation_error
    normalized_archive = _public_site_trim(archive, 20).lower() or "active"
    if normalized_archive not in {"active", "archived", "all"}:
        return _error_response(400, "invalid_archive_filter", "archive must be active, archived, or all.")
    normalized_lead_status = _public_site_trim(leadStatus, 20).lower()
    if normalized_lead_status and normalized_lead_status not in {"partial", "abandoned", "submitted"}:
        return _error_response(400, "invalid_lead_status", "Unsupported lead status.")

    db = SessionLocal()
    try:
        rows = (
            _site_analytics_filtered_leads_query(
                db,
                start_at=start_at,
                end_at=end_at,
                status=normalized_lead_status or None,
                archive=normalized_archive,
                path=path,
            )
            .order_by(PublicLeadDraft.updated_at.desc())
            .limit(PUBLIC_ANALYTICS_MAX_LEAD_EXPORT_ROWS)
            .all()
        )
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["status", "first_name", "last_name", "email", "phone", "message", "product_interest", "source_path", "source_cta", "submitted_at", "updated_at", "archived_at"])
        for row in rows:
            writer.writerow(
                [
                    _audit_csv_cell(getattr(row, "status", None)),
                    _audit_csv_cell(getattr(row, "first_name", None)),
                    _audit_csv_cell(getattr(row, "last_name", None)),
                    _audit_csv_cell(getattr(row, "email", None)),
                    _audit_csv_cell(getattr(row, "phone", None)),
                    _audit_csv_cell(getattr(row, "message", None)),
                    _audit_csv_cell(getattr(row, "product_interest", None)),
                    _audit_csv_cell(getattr(row, "source_path", None)),
                    _audit_csv_cell(getattr(row, "source_cta", None)),
                    _audit_csv_cell(_iso_datetime(getattr(row, "submitted_at", None))),
                    _audit_csv_cell(_iso_datetime(getattr(row, "updated_at", None))),
                    _audit_csv_cell(_iso_datetime(getattr(row, "archived_at", None))),
                ]
            )
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="site-leads.csv"'},
        )
    except Exception:
        logger.exception("[site_analytics] lead CSV export failed.")
        return _error_response(500, "site_analytics_export_failed", "Unable to export leads.")
    finally:
        db.close()


@app.post("/api/admin/site-analytics/leads/{lead_id}/archive")
async def archive_site_analytics_lead(lead_id: str, request: Request) -> JSONResponse:
    current_user, current_admin_access, error_response = _require_admin_permission(request, PERMISSION_SITE_ANALYTICS_WRITE)
    if error_response:
        return error_response
    try:
        lead_uuid = UUID(lead_id)
    except (TypeError, ValueError):
        return _error_response(400, "invalid_lead_id", "Lead ID is invalid.")
    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    db = SessionLocal()
    try:
        lead = db.query(PublicLeadDraft).filter(PublicLeadDraft.id == lead_uuid).first()
        if not lead:
            return _error_response(404, "lead_not_found", "Lead capture was not found.")
        lead.archived_at = datetime.now(timezone.utc)
        lead.archived_by_user_id = _clean_text(current_user.get("id")) or None
        lead.archive_reason = _public_site_trim(body.get("reason"), 300) or None
        lead.updated_at = datetime.now(timezone.utc)
        db.commit()
        _record_admin_audit_event(
            db,
            request,
            "site_lead.archived",
            target_type="public_lead",
            target_id=lead.id,
            metadata={"status": _clean_text(getattr(lead, "status", None))},
            admin_auth_user=current_user,
            admin_access=current_admin_access,
        )
        return JSONResponse({"ok": True, "lead": _site_analytics_lead_payload(lead)})
    except Exception:
        db.rollback()
        logger.exception("[site_analytics] lead archive failed lead_id=%s", lead_id)
        return _error_response(500, "site_lead_archive_failed", "Unable to archive lead capture.")
    finally:
        db.close()


@app.post("/api/admin/site-analytics/leads/{lead_id}/unarchive")
async def unarchive_site_analytics_lead(lead_id: str, request: Request) -> JSONResponse:
    current_user, current_admin_access, error_response = _require_admin_permission(request, PERMISSION_SITE_ANALYTICS_WRITE)
    if error_response:
        return error_response
    try:
        lead_uuid = UUID(lead_id)
    except (TypeError, ValueError):
        return _error_response(400, "invalid_lead_id", "Lead ID is invalid.")
    db = SessionLocal()
    try:
        lead = db.query(PublicLeadDraft).filter(PublicLeadDraft.id == lead_uuid).first()
        if not lead:
            return _error_response(404, "lead_not_found", "Lead capture was not found.")
        lead.archived_at = None
        lead.archived_by_user_id = None
        lead.archive_reason = None
        lead.updated_at = datetime.now(timezone.utc)
        db.commit()
        _record_admin_audit_event(
            db,
            request,
            "site_lead.unarchived",
            target_type="public_lead",
            target_id=lead.id,
            metadata={"status": _clean_text(getattr(lead, "status", None))},
            admin_auth_user=current_user,
            admin_access=current_admin_access,
        )
        return JSONResponse({"ok": True, "lead": _site_analytics_lead_payload(lead)})
    except Exception:
        db.rollback()
        logger.exception("[site_analytics] lead unarchive failed lead_id=%s", lead_id)
        return _error_response(500, "site_lead_unarchive_failed", "Unable to restore lead capture.")
    finally:
        db.close()


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
    current_user, current_admin_access, error_response = _require_admin_permission(request, PERMISSION_ADMIN_MANAGEMENT_WRITE)
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
        _record_admin_audit_event(
            db,
            request,
            "admin_access.created",
            target_type="admin_user",
            target_id=target_user_id,
            metadata={
                "targetEmail": email,
                "role": role,
                "existingUser": bool(auth_user.get("existing")),
                "inviteSent": bool(auth_user.get("invited")),
            },
            admin_auth_user=current_user,
            admin_access=current_admin_access,
        )
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
    current_user, current_admin_access, error_response = _require_admin_permission(request, PERMISSION_ADMIN_MANAGEMENT_WRITE)
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
        previous_display_name = _clean_text(getattr(admin_user, "display_name", None))
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
        audit_metadata: dict[str, object] = {"changedFields": []}
        changed_fields = audit_metadata["changedFields"]
        if isinstance(changed_fields, list):
            if has_role_update:
                changed_fields.append("role")
                audit_metadata["previousRole"] = current_role
                audit_metadata["newRole"] = _clean_text(getattr(admin_user, "role", None))
            if has_status_update:
                changed_fields.append("status")
                audit_metadata["previousStatus"] = current_status
                audit_metadata["newStatus"] = _clean_text(getattr(admin_user, "status", None))
            if has_name_update:
                changed_fields.append("name")
                audit_metadata["nameChanged"] = previous_display_name != _clean_text(getattr(admin_user, "display_name", None))
        _record_admin_audit_event(
            db,
            request,
            "admin_access.updated",
            target_type="admin_user",
            target_id=target_user_id,
            metadata=audit_metadata,
            admin_auth_user=current_user,
            admin_access=current_admin_access,
        )
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
                str(row[0]).strip().lower()
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
            clients_query = clients_query.filter(func.lower(ClientSubmission.user_email).in_(matching_emails))
        submission_client_rows = (
            clients_query.group_by(ClientSubmission.user_email)
            .order_by(func.max(ClientSubmission.submitted_at).desc())
            .all()
        )
        submission_client_emails = {
            (_clean_text(row.email) or "").lower()
            for row in submission_client_rows
            if _clean_text(row.email)
        }

        user_query = db.query(User)
        if matching_emails is not None:
            user_query = user_query.filter(func.lower(User.email).in_(matching_emails))
        user_rows = user_query.order_by(func.lower(User.email).asc()).all()

        combined_client_rows: list[dict[str, Any]] = [
            {
                "email": _clean_text(row.email) or "",
                "submission_count": int(row.submission_count or 0),
                "last_submitted_at": row.last_submitted_at,
            }
            for row in submission_client_rows
            if _clean_text(row.email)
        ]
        for user in user_rows:
            user_email = _clean_text(getattr(user, "email", None)) or ""
            normalized_user_email = user_email.lower()
            if not user_email or normalized_user_email in submission_client_emails:
                continue
            combined_client_rows.append(
                {
                    "email": user_email,
                    "submission_count": 0,
                    "last_submitted_at": None,
                }
            )

        has_more = len(combined_client_rows) > offset + safe_limit
        client_rows = combined_client_rows[offset:offset + safe_limit]
        client_emails = [row["email"] for row in client_rows if row.get("email")]

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
                    func.lower(ClientSubmission.user_email).label("email"),
                    func.count(Upload.id).label("upload_count"),
                )
                .outerjoin(
                    Upload,
                    and_(
                        Upload.submission_id == ClientSubmission.id,
                        Upload.voided_at.is_(None),
                    ),
                )
                .filter(func.lower(ClientSubmission.user_email).in_(normalized_client_emails))
                .group_by(func.lower(ClientSubmission.user_email))
                .all()
            )
            upload_counts = {row.email: int(row.upload_count or 0) for row in upload_count_rows if row.email}

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
                    summary["checkoutSessionCount"] += 1
                    if _checkout_session_is_paid_or_complete(session):
                        summary["paidCheckoutSessionCount"] += 1
                    if _checkout_session_is_expired(session):
                        summary["expiredCheckoutSessionCount"] += 1
                    if _checkout_session_is_open_or_unpaid(session):
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
                .filter(func.lower(ClientSubmission.user_email).in_(normalized_client_emails))
                .order_by(
                    func.lower(ClientSubmission.user_email).asc(),
                    ClientSubmission.submitted_at.desc(),
                )
                .all()
            )
            for submission in latest_rows:
                submission_email = (_clean_text(getattr(submission, "user_email", None)) or "").lower()
                if submission_email and submission_email not in latest_submissions:
                    latest_submissions[submission_email] = submission

            users = db.query(User).filter(func.lower(User.email).in_(normalized_client_emails)).all()
            users_by_email = {
                (_clean_text(getattr(user, "email", None)) or "").lower(): user
                for user in users
                if _clean_text(getattr(user, "email", None))
            }

        items = []
        for row in client_rows:
            email = row["email"] or ""
            normalized_email = email.lower()
            latest_submission = latest_submissions.get(normalized_email)
            user_record = users_by_email.get(normalized_email)
            if latest_submission:
                latest_name = _full_name(latest_submission)
                latest_office_name = _clean_text(getattr(latest_submission, "office_name", None))
                latest_org_type = _clean_text(getattr(latest_submission, "org_type", None))
            else:
                latest_name = _user_full_name(user_record)
                latest_office_name = _clean_text(getattr(user_record, "office_name", None))
                latest_org_type = _clean_text(getattr(user_record, "org_type", None))
            latest_phone = (
                _clean_text(getattr(latest_submission, "phone", None))
                or _clean_text(getattr(user_record, "phone", None))
                or None
            )
            items.append(
                {
                    "email": email,
                    "latestName": latest_name,
                    "latestOfficeName": latest_office_name,
                    "latestOrgType": latest_org_type,
                    "latestPhone": latest_phone,
                    "submissionCount": int(row["submission_count"] or 0),
                    "uploadCount": upload_counts.get(normalized_email, 0),
                    "latestSubmittedAt": _iso_datetime(row["last_submitted_at"]),
                    "latestStatus": _clean_text(getattr(latest_submission, "status", None)),
                    "billing": billing_summaries.get(normalized_email, _empty_billing_summary()),
                }
            )

        return _clients_response(items, safe_limit, offset, has_more=has_more)
    except Exception:
        logger.exception("[admin_api] client list query failed.")
        return _error_response(500, "internal_error", "Unable to load clients.")
    finally:
        db.close()


@app.post("/api/admin/clients")
async def create_admin_client(request: Request) -> JSONResponse:
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_CLIENTS_WRITE)
    if error_response:
        return error_response

    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    client_email, validation_error = _required_admin_access_email(body.get("email"))
    if validation_error:
        return validation_error
    first_name, validation_error = _required_limited_text(body.get("firstName"), "firstName", 255)
    if validation_error:
        return validation_error
    last_name, validation_error = _required_limited_text(body.get("lastName"), "lastName", 255)
    if validation_error:
        return validation_error
    office_name, validation_error = _required_limited_text(body.get("officeName"), "officeName", 255)
    if validation_error:
        return validation_error
    org_type, validation_error = _required_limited_text(body.get("orgType"), "orgType", 50)
    if validation_error:
        return validation_error
    phone, validation_error = _optional_limited_text(body.get("phone"), "phone", 50)
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        existing_user = db.query(User.id).filter(func.lower(User.email) == client_email).first()
        if existing_user:
            return _error_response(409, "client_already_exists", "A client already exists for this email.")

        user = User(
            email=client_email,
            first_name=first_name,
            last_name=last_name,
            office_name=office_name,
            org_type=org_type,
            phone=phone,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info(
            "[admin_api] manual client created client_email=%s admin_user_id=%s",
            client_email,
            str(admin_auth_user.get("id") or ""),
        )
        _record_admin_audit_event(
            db,
            request,
            "client.created",
            target_type="client",
            target_id=getattr(user, "id", None),
            client_email=client_email,
            metadata={
                "officeName": office_name,
                "orgType": org_type,
            },
            admin_auth_user=admin_auth_user,
            admin_access=admin_access,
        )
        return JSONResponse(
            {
                "ok": True,
                "client": {
                    "id": _id_text(getattr(user, "id", None)),
                    "email": _clean_text(getattr(user, "email", None)),
                    "firstName": _clean_text(getattr(user, "first_name", None)),
                    "lastName": _clean_text(getattr(user, "last_name", None)),
                    "officeName": _clean_text(getattr(user, "office_name", None)),
                    "orgType": _clean_text(getattr(user, "org_type", None)),
                    "phone": _clean_text(getattr(user, "phone", None)),
                    "submissionCount": 0,
                    "uploadCount": 0,
                },
            }
        )
    except IntegrityError:
        db.rollback()
        return _error_response(409, "client_already_exists", "A client already exists for this email.")
    except Exception:
        db.rollback()
        logger.exception("[admin_api] manual client create failed client_email=%s", client_email)
        return _error_response(500, "client_create_failed", "Unable to create client.")
    finally:
        db.close()


@app.post("/api/admin/uploads/{upload_id}/void")
async def void_admin_upload(request: Request, upload_id: str) -> JSONResponse:
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_UPLOADS_WRITE)
    if error_response:
        return error_response

    upload_uuid, validation_error = _required_uuid(upload_id, "upload_id")
    if validation_error:
        return validation_error

    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    reason, validation_error = _required_limited_text(body.get("reason"), "reason", 500)
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        upload = db.query(Upload).filter(Upload.id == upload_uuid).first()
        if not upload:
            return _error_response(404, "upload_not_found", "Upload was not found.")

        if _upload_is_voided(upload):
            return JSONResponse({"ok": True, "upload": _upload_payload(upload)})

        if bool(getattr(upload, "paid", False)):
            return _error_response(
                409,
                "paid_upload_cannot_be_voided",
                "Paid uploads cannot be voided.",
            )

        if _upload_has_paid_checkout_session(db, upload_uuid):
            return _error_response(
                409,
                "paid_checkout_upload_cannot_be_voided",
                "Uploads linked to paid checkout sessions cannot be voided.",
            )

        now = datetime.now(timezone.utc)
        upload.voided_at = now
        upload.voided_by_admin_user_id = _clean_text((admin_auth_user or {}).get("id"))
        upload.voided_by_admin_email = (
            _clean_text(getattr(admin_access, "email", None))
            or _clean_text((admin_auth_user or {}).get("email"))
        )
        upload.void_reason = reason
        db.commit()
        db.refresh(upload)
        logger.info(
            "[admin_api] upload voided upload_id=%s admin_user_id=%s",
            upload_uuid,
            _clean_text((admin_auth_user or {}).get("id")),
        )
        _record_admin_audit_event(
            db,
            request,
            "upload.voided",
            target_type="upload",
            target_id=upload_uuid,
            client_email=_clean_text(getattr(upload, "user_email", None)),
            metadata={
                "reason": reason,
                "paid": False,
                "toolName": _clean_text(getattr(upload, "tool_name", None)),
            },
            admin_auth_user=admin_auth_user,
            admin_access=admin_access,
        )
        return JSONResponse({"ok": True, "upload": _upload_payload(upload)})
    except Exception:
        db.rollback()
        logger.exception("[admin_api] upload void failed upload_id=%s", upload_uuid)
        return _error_response(500, "upload_void_failed", "Unable to void upload.")
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


@app.get("/api/admin/agreements")
def list_admin_agreements(
    request: Request,
    clientEmail: Optional[str] = None,
    status: Optional[str] = None,
    documentType: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_AGREEMENTS_READ)
    if error_response:
        return error_response

    normalized_status = (_clean_text(status) or "").lower()
    if normalized_status and normalized_status not in {"draft", "sent", "pending_ba_signature", "signed", "voided", "superseded", "expired"}:
        return _error_response(400, "invalid_status", "status is not supported.")
    normalized_document_type = _clean_text(documentType) or AGREEMENT_DOCUMENT_TYPE
    if normalized_document_type != AGREEMENT_DOCUMENT_TYPE:
        return _error_response(400, "invalid_document_type", "documentType is not supported.")
    normalized_client_email = normalize_agreement_email(clientEmail)
    if clientEmail and not normalized_client_email:
        return _error_response(400, "invalid_client_email", "clientEmail must be a valid email.")
    normalized_search = _clean_text(search)
    safe_limit = min(limit, 100)

    db = SessionLocal()
    try:
        query = db.query(ConsultingAgreement).filter(ConsultingAgreement.document_type == normalized_document_type)
        if normalized_client_email:
            query = query.filter(func.lower(ConsultingAgreement.client_email) == normalized_client_email)
        if normalized_status:
            query = query.filter(ConsultingAgreement.status == normalized_status)
        if normalized_search:
            search_like = f"%{normalized_search}%"
            query = query.filter(
                or_(
                    ConsultingAgreement.client_email.ilike(search_like),
                    ConsultingAgreement.client_legal_name.ilike(search_like),
                    ConsultingAgreement.office_name.ilike(search_like),
                    ConsultingAgreement.signer_email.ilike(search_like),
                    ConsultingAgreement.signer_name.ilike(search_like),
                )
            )

        rows = (
            query.order_by(ConsultingAgreement.created_at.desc(), ConsultingAgreement.id.desc())
            .offset(offset)
            .limit(safe_limit + 1)
            .all()
        )
        has_more = len(rows) > safe_limit
        rows = rows[:safe_limit]
        return JSONResponse(
            {
                "ok": True,
                "items": [_agreement_payload(row) for row in rows],
                "count": len(rows),
                "limit": safe_limit,
                "offset": offset,
                "hasMore": has_more,
            }
        )
    except Exception:
        logger.exception("[agreements] list failed")
        return _error_response(500, "agreements_lookup_failed", "Unable to load agreements.")
    finally:
        db.close()


@app.get("/api/admin/agreements/{agreement_id}")
def get_admin_agreement(request: Request, agreement_id: str) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_AGREEMENTS_READ)
    if error_response:
        return error_response

    agreement_uuid, validation_error = _required_uuid(agreement_id, "agreementId")
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        agreement = db.query(ConsultingAgreement).filter(ConsultingAgreement.id == agreement_uuid).first()
        if not agreement:
            return _error_response(404, "agreement_not_found", "Agreement was not found.")
        return JSONResponse({"ok": True, "agreement": _agreement_payload(agreement, include_snapshot=True)})
    except Exception:
        logger.exception("[agreements] get failed agreement_id=%s", agreement_uuid)
        return _error_response(500, "agreement_lookup_failed", "Unable to load agreement.")
    finally:
        db.close()


@app.post("/api/admin/agreements/preview")
async def preview_admin_agreement(request: Request) -> Response:
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_AGREEMENTS_WRITE)
    if error_response:
        return error_response
    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    try:
        payload = normalize_admin_agreement_payload(body)
        pdf_bytes, template_sha = render_agreement_pdf(payload)
    except AgreementServiceError as exc:
        return _agreement_error_response(exc)
    except Exception:
        logger.exception("[agreements] preview failed")
        return _error_response(500, "agreement_preview_failed", "Unable to generate agreement preview.")

    db = SessionLocal()
    try:
        _record_admin_audit_event(
            db,
            request,
            "agreement.previewed",
            target_type="consulting_agreement",
            client_email=payload["client_email"],
            metadata={
                "documentType": payload["document_type"],
                "templateVersion": AGREEMENT_TEMPLATE_VERSION,
                "sourceTemplateSha256Present": bool(template_sha),
                "effectiveDate": payload["effective_date"].isoformat(),
            },
            admin_auth_user=admin_auth_user,
            admin_access=admin_access,
        )
    finally:
        db.close()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="baa-privacy-agreement-preview.pdf"'},
    )


@app.post("/api/admin/agreements/send")
async def send_admin_agreement(request: Request) -> JSONResponse:
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_AGREEMENTS_WRITE)
    if error_response:
        return error_response
    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    if not agreement_email_configured():
        return _error_response(
            500,
            "agreement_email_not_configured",
            "SendGrid agreement email configuration is missing.",
        )

    try:
        payload = normalize_admin_agreement_payload(body)
        draft_pdf_bytes, template_sha = render_agreement_pdf(payload)
        raw_token, token_hash, token_expires_at = generate_signer_token()
    except AgreementServiceError as exc:
        return _agreement_error_response(exc)
    except Exception:
        logger.exception("[agreements] send preparation failed")
        return _error_response(500, "agreement_send_failed", "Unable to prepare agreement.")

    agreement_id = uuid4()
    draft_pdf_path = f"agreements/{agreement_id}/draft.pdf"
    try:
        upload_agreement_file(draft_pdf_path, draft_pdf_bytes, "application/pdf")
    except AgreementServiceError as exc:
        return _agreement_error_response(exc)

    db = SessionLocal()
    try:
        client_user_id = _agreement_client_user_id(db, payload)
        now = agreement_utcnow()
        admin_user_id = _clean_text((admin_auth_user or {}).get("id"))
        admin_email = (
            _clean_text(getattr(admin_access, "email", None))
            or _clean_text((admin_auth_user or {}).get("email"))
        )
        agreement = ConsultingAgreement(
            id=agreement_id,
            client_email=payload["client_email"],
            client_user_id=client_user_id,
            client_legal_name=payload["client_legal_name"],
            office_name=payload.get("office_name"),
            org_type=payload.get("org_type"),
            phone=payload.get("phone"),
            state=payload["state"],
            effective_date=payload["effective_date"],
            document_type=payload["document_type"],
            status="draft",
            is_current=False,
            template_version=AGREEMENT_TEMPLATE_VERSION,
            template_snapshot=build_template_snapshot(payload, template_sha),
            source_template_path=str(AGREEMENT_TEMPLATE_PATH),
            source_template_sha256=template_sha,
            draft_pdf_path=draft_pdf_path,
            signer_token_hash=token_hash,
            signer_token_expires_at=token_expires_at,
            signer_name=payload.get("signer_name"),
            signer_email=payload["signer_email"],
            signer_title=payload.get("signer_title"),
            ba_signer_name=payload.get("ba_signer_name"),
            ba_signer_title=payload.get("ba_signer_title"),
            ba_signer_email=payload.get("ba_signer_email"),
            ba_signature_mode="tokenized_link",
            created_by_admin_id=admin_user_id,
            created_by_admin_email=admin_email,
            sent_by_admin_id=admin_user_id,
            sent_by_admin_email=admin_email,
            created_at=now,
            updated_at=now,
        )
        db.add(agreement)
        db.commit()
        db.refresh(agreement)

        signing_url = build_signing_url(raw_token)
        try:
            send_agreement_signature_request_email(
                payload["signer_email"],
                signing_url,
                client_legal_name=payload["client_legal_name"],
                expires_at=token_expires_at,
            )
        except AgreementServiceError as exc:
            return _agreement_error_response(exc)
        except Exception:
            logger.exception("[agreements] signature request email failed agreement_id=%s", agreement_id)
            return _error_response(502, "agreement_email_send_failed", "Agreement email could not be sent.")

        agreement.status = "sent"
        agreement.sent_at = agreement_utcnow()
        agreement.updated_at = agreement.sent_at
        db.commit()
        db.refresh(agreement)

        _record_admin_audit_event(
            db,
            request,
            "agreement.sent",
            target_type="consulting_agreement",
            target_id=agreement.id,
            client_email=agreement.client_email,
            metadata={
                "documentType": agreement.document_type,
                "templateVersion": agreement.template_version,
                "effectiveDate": agreement.effective_date.isoformat(),
                "signerEmail": agreement.signer_email,
                "baSignerEmail": agreement.ba_signer_email,
                "tokenExpiresAt": _iso_datetime(agreement.signer_token_expires_at),
            },
            admin_auth_user=admin_auth_user,
            admin_access=admin_access,
        )
        return JSONResponse({"ok": True, "agreement": _agreement_payload(agreement)})
    except Exception:
        db.rollback()
        logger.exception("[agreements] send failed agreement_id=%s", agreement_id)
        return _error_response(500, "agreement_send_failed", "Unable to send agreement.")
    finally:
        db.close()


@app.post("/api/admin/agreements/{agreement_id}/download-url")
async def create_admin_agreement_download_url(request: Request, agreement_id: str) -> JSONResponse:
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_AGREEMENTS_READ)
    if error_response:
        return error_response

    agreement_uuid, validation_error = _required_uuid(agreement_id, "agreementId")
    if validation_error:
        return validation_error
    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error
    file_type = (_clean_text(body.get("fileType") or body.get("document") or body.get("target")) or "").lower()

    db = SessionLocal()
    try:
        agreement = db.query(ConsultingAgreement).filter(ConsultingAgreement.id == agreement_uuid).first()
        if not agreement:
            return _error_response(404, "agreement_not_found", "Agreement was not found.")
        if not file_type:
            file_type = "signed" if _clean_text(getattr(agreement, "signed_pdf_path", None)) else "draft"
        if file_type not in {"draft", "signed"}:
            return _error_response(400, "invalid_file_type", "fileType must be draft or signed.")
        object_path = agreement.signed_pdf_path if file_type == "signed" else agreement.draft_pdf_path
        if not object_path:
            return _error_response(404, "agreement_file_not_found", "Requested agreement file is not available.")
        try:
            signed_url = create_agreement_signed_url(object_path, AGREEMENTS_SIGNED_URL_TTL_SECONDS)
        except AgreementServiceError as exc:
            return _agreement_error_response(exc)
        if not signed_url:
            return _error_response(502, "agreement_signed_url_failed", "Unable to create agreement download URL.")

        _record_admin_audit_event(
            db,
            request,
            "agreement.download_url_created",
            target_type="consulting_agreement",
            target_id=agreement.id,
            client_email=agreement.client_email,
            metadata={
                "documentType": agreement.document_type,
                "fileType": file_type,
                "status": agreement.status,
            },
            admin_auth_user=admin_auth_user,
            admin_access=admin_access,
        )
        return JSONResponse(
            {
                "ok": True,
                "url": signed_url,
                "expiresInSeconds": AGREEMENTS_SIGNED_URL_TTL_SECONDS,
                "fileType": file_type,
            }
        )
    except Exception:
        logger.exception("[agreements] download url failed agreement_id=%s", agreement_uuid)
        return _error_response(500, "agreement_download_url_failed", "Unable to create agreement download URL.")
    finally:
        db.close()


@app.post("/api/admin/agreements/{agreement_id}/void")
async def void_admin_agreement(request: Request, agreement_id: str) -> JSONResponse:
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_AGREEMENTS_WRITE)
    if error_response:
        return error_response

    agreement_uuid, validation_error = _required_uuid(agreement_id, "agreementId")
    if validation_error:
        return validation_error
    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error
    reason, validation_error = _required_reason(body.get("reason"))
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        agreement = db.query(ConsultingAgreement).filter(ConsultingAgreement.id == agreement_uuid).first()
        if not agreement:
            return _error_response(404, "agreement_not_found", "Agreement was not found.")
        if agreement.status == "voided":
            return _error_response(409, "agreement_already_voided", "Agreement is already voided.")

        now = agreement_utcnow()
        previous_status = agreement.status
        agreement.status = "voided"
        agreement.is_current = False
        agreement.voided_at = now
        agreement.voided_by_admin_id = _clean_text((admin_auth_user or {}).get("id"))
        agreement.voided_by_admin_email = (
            _clean_text(getattr(admin_access, "email", None))
            or _clean_text((admin_auth_user or {}).get("email"))
        )
        agreement.void_reason = reason
        agreement.updated_at = now
        db.commit()
        db.refresh(agreement)
        _record_admin_audit_event(
            db,
            request,
            "agreement.voided",
            target_type="consulting_agreement",
            target_id=agreement.id,
            client_email=agreement.client_email,
            metadata={
                "documentType": agreement.document_type,
                "previousStatus": previous_status,
                "reason": reason,
            },
            admin_auth_user=admin_auth_user,
            admin_access=admin_access,
        )
        return JSONResponse({"ok": True, "agreement": _agreement_payload(agreement)})
    except Exception:
        db.rollback()
        logger.exception("[agreements] void failed agreement_id=%s", agreement_uuid)
        return _error_response(500, "agreement_void_failed", "Unable to void agreement.")
    finally:
        db.close()


@app.post("/api/agreements/session")
async def create_public_agreement_session(request: Request) -> JSONResponse:
    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error
    raw_token = _clean_text(body.get("token"))
    if not raw_token:
        return _agreement_public_error("token_required", "Signing token is required.", status=400)

    token_hash = hash_signer_token(raw_token)
    db = SessionLocal()
    try:
        agreement, signer_role = _find_public_agreement_by_token_hash(db, token_hash)
        validation_error = _validate_public_signable_agreement(agreement, signer_role)
        if validation_error:
            return validation_error

        now = agreement_utcnow()
        opened_field = "ba_opened_at" if signer_role == "ba" else "opened_at"
        if not getattr(agreement, opened_field, None):
            setattr(agreement, opened_field, now)
            agreement.updated_at = now
            db.commit()
            db.refresh(agreement)
            _record_admin_audit_event(
                db,
                request,
                "agreement.opened",
                target_type="consulting_agreement",
                target_id=agreement.id,
                client_email=agreement.client_email,
                metadata={
                    "documentType": agreement.document_type,
                    "status": agreement.status,
                    "signerRole": signer_role,
                },
                source="agreements_public",
            )

        try:
            draft_pdf_url = create_agreement_signed_url(agreement.draft_pdf_path, AGREEMENTS_SIGNED_URL_TTL_SECONDS)
        except AgreementServiceError as exc:
            return _agreement_error_response(exc)
        if not draft_pdf_url:
            return _agreement_public_error("agreement_preview_unavailable", "Agreement preview is unavailable.", status=502)

        expires_at = agreement.ba_signer_token_expires_at if signer_role == "ba" else agreement.signer_token_expires_at
        session_signer_name = agreement.ba_signer_name if signer_role == "ba" else agreement.signer_name
        session_signer_email = agreement.ba_signer_email if signer_role == "ba" else agreement.signer_email
        session_signer_title = agreement.ba_signer_title if signer_role == "ba" else agreement.signer_title
        return JSONResponse(
            {
                "ok": True,
                "agreement": {
                    "id": _id_text(agreement.id),
                    "documentType": agreement.document_type,
                    "status": agreement.status,
                    "signerRole": signer_role,
                    "clientLegalName": agreement.client_legal_name,
                    "effectiveDate": _iso_date(agreement.effective_date),
                    "signerName": session_signer_name,
                    "signerEmail": session_signer_email,
                    "signerTitle": session_signer_title,
                    "expiresAt": _iso_datetime(expires_at),
                    "sentAt": _iso_datetime(agreement.sent_at),
                    "openedAt": _iso_datetime(getattr(agreement, opened_field, None)),
                    "draftPdfUrl": draft_pdf_url,
                    "signedPdfUrl": None,
                },
                "expiresInSeconds": AGREEMENTS_SIGNED_URL_TTL_SECONDS,
            }
        )
    except Exception:
        logger.exception("[agreements_public] session failed")
        return _agreement_public_error("agreement_session_failed", "Agreement session could not be loaded.", status=500)
    finally:
        db.close()


@app.post("/api/agreements/sign")
async def sign_public_agreement(request: Request) -> JSONResponse:
    body, parse_error = await _request_json_body(request)
    if parse_error:
        return parse_error

    raw_token = _clean_text(body.get("token"))
    if not raw_token:
        return _agreement_public_error("token_required", "Signing token is required.", status=400)
    signer_name = _clean_text(body.get("typedSignerName") or body.get("typed_name") or body.get("signerName"))
    if not signer_name:
        return _agreement_public_error("signer_name_required", "Signer name is required.", status=400)
    signer_title = _clean_text(body.get("signerTitle") or body.get("title") or body.get("typedSignerTitle"))
    if not signer_title:
        return _agreement_public_error("signer_title_required", "Signer title is required.", status=400)
    accepted = _truthy(body.get("accepted") or body.get("agreementAccepted"))
    if not accepted:
        return _agreement_public_error("agreement_acceptance_required", "Agreement acceptance is required.", status=400)
    authority_confirmed = _truthy(body.get("authorityConfirmed") or body.get("signerAuthorityConfirmed"))
    if not authority_confirmed:
        return _agreement_public_error("signer_authority_required", "Signer authority confirmation is required.", status=400)
    try:
        signature = parse_signature_image(
            body.get("signatureImage")
            or body.get("signature_image")
            or body.get("signatureImageDataUrl")
            or body.get("signature_image_data_url")
        )
    except AgreementServiceError as exc:
        return _agreement_error_response(exc)

    token_hash = hash_signer_token(raw_token)
    db = SessionLocal()
    try:
        agreement, signer_role = _find_public_agreement_by_token_hash(db, token_hash)
        validation_error = _validate_public_signable_agreement(agreement, signer_role)
        if validation_error:
            return validation_error

        signed_at = agreement_utcnow()
        signer_ip = _request_client_ip(request)
        signer_user_agent = _request_user_agent(request)

        if signer_role == "client":
            locked_agreement = (
                db.query(ConsultingAgreement)
                .filter(ConsultingAgreement.id == agreement.id)
                .with_for_update()
                .first()
            )
            validation_error = _validate_public_signable_agreement(locked_agreement, signer_role)
            if validation_error:
                db.rollback()
                return validation_error

            ba_signer_name = _clean_text(getattr(locked_agreement, "ba_signer_name", None))
            ba_signer_title = _clean_text(getattr(locked_agreement, "ba_signer_title", None))
            ba_signer_email = normalize_agreement_email(getattr(locked_agreement, "ba_signer_email", None))
            if not ba_signer_name or not ba_signer_title or not ba_signer_email:
                db.rollback()
                logger.warning(
                    "[agreements_public] BA signer setup missing agreement_id=%s has_name=%s has_title=%s has_email=%s",
                    locked_agreement.id,
                    bool(ba_signer_name),
                    bool(ba_signer_title),
                    bool(ba_signer_email),
                )
                return _agreement_public_error(
                    "ba_signer_required",
                    "BA countersign setup is incomplete. Please contact alphaSource Consulting.",
                    status=409,
                )

            if not agreement_email_configured():
                db.rollback()
                logger.error(
                    "[agreements_public] BA countersign email config missing agreement_id=%s has_sendgrid=%s has_from_email=%s has_signer_base_url=%s",
                    locked_agreement.id,
                    bool(os.getenv("SENDGRID_API_KEY")),
                    bool(os.getenv("FROM_EMAIL")),
                    bool(os.getenv("AGREEMENTS_SIGNER_BASE_URL")),
                )
                return _agreement_public_error(
                    "agreement_email_not_configured",
                    "Agreement email delivery is not configured. Please contact alphaSource Consulting.",
                    status=503,
                )

            raw_ba_token, ba_token_hash, ba_token_expires_at = generate_signer_token()
            signature_path = f"agreements/{locked_agreement.id}/signatures/client.{signature['extension']}"

            try:
                upload_agreement_file(signature_path, signature["buffer"], signature["mime"], upsert=True)
                send_agreement_ba_countersign_request_email(
                    ba_signer_email,
                    build_signing_url(raw_ba_token),
                    client_legal_name=locked_agreement.client_legal_name,
                    expires_at=ba_token_expires_at,
                )
            except AgreementServiceError as exc:
                db.rollback()
                logger.warning(
                    "[agreements_public] client signature prerequisite failed agreement_id=%s code=%s status=%s",
                    locked_agreement.id,
                    exc.code,
                    exc.status,
                )
                if exc.code == "agreement_email_not_configured":
                    return _agreement_public_error(
                        exc.code,
                        "Agreement email delivery is not configured. Please contact alphaSource Consulting.",
                        status=503,
                    )
                if exc.code == "agreement_storage_upload_failed":
                    return _agreement_public_error(
                        exc.code,
                        "Agreement signature could not be saved. Please contact alphaSource Consulting.",
                        status=502,
                    )
                return _agreement_public_error(exc.code, exc.message, status=exc.status)
            except Exception:
                db.rollback()
                logger.exception(
                    "[agreements_public] BA countersign email failed agreement_id=%s error_type=%s sendgrid_status=%s",
                    locked_agreement.id,
                    type(exc).__name__,
                    getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None),
                )
                return _agreement_public_error(
                    "agreement_ba_email_send_failed",
                    "BA countersign email could not be sent. Please contact alphaSource Consulting.",
                    status=502,
                )

            locked_agreement.status = "pending_ba_signature"
            locked_agreement.opened_at = locked_agreement.opened_at or signed_at
            locked_agreement.signer_name = signer_name
            locked_agreement.signer_title = signer_title
            locked_agreement.signer_authority_confirmed = True
            locked_agreement.signer_accepted = True
            locked_agreement.client_signed_at = signed_at
            locked_agreement.signer_ip = signer_ip
            locked_agreement.signer_user_agent = signer_user_agent
            locked_agreement.signature_image_path = signature_path
            locked_agreement.signature_sha256 = signature["sha256"]
            locked_agreement.client_signature_image_path = signature_path
            locked_agreement.client_signature_sha256 = signature["sha256"]
            locked_agreement.ba_signer_name = ba_signer_name
            locked_agreement.ba_signer_title = ba_signer_title
            locked_agreement.ba_signer_email = ba_signer_email
            locked_agreement.ba_signer_token_hash = ba_token_hash
            locked_agreement.ba_signer_token_expires_at = ba_token_expires_at
            locked_agreement.updated_at = signed_at
            snapshot = dict(locked_agreement.template_snapshot or {})
            snapshot["clientSignature"] = {
                "signedAt": signed_at.isoformat(),
                "signerName": signer_name,
                "signerTitle": signer_title,
                "signerAccepted": True,
                "signerAuthorityConfirmed": True,
                "signatureSha256": signature["sha256"],
            }
            locked_agreement.template_snapshot = snapshot
            db.commit()
            db.refresh(locked_agreement)

            _record_admin_audit_event(
                db,
                request,
                "agreement.client_signed",
                target_type="consulting_agreement",
                target_id=locked_agreement.id,
                client_email=locked_agreement.client_email,
                metadata={
                    "documentType": locked_agreement.document_type,
                    "templateVersion": locked_agreement.template_version,
                    "signerRole": "client",
                    "signerEmail": locked_agreement.signer_email,
                    "signerTitle": locked_agreement.signer_title,
                },
                source="agreements_public",
            )

            _record_admin_audit_event(
                db,
                request,
                "agreement.ba_signature_requested",
                target_type="consulting_agreement",
                target_id=locked_agreement.id,
                client_email=locked_agreement.client_email,
                metadata={
                    "documentType": locked_agreement.document_type,
                    "baSignerEmail": locked_agreement.ba_signer_email,
                    "tokenExpiresAt": _iso_datetime(locked_agreement.ba_signer_token_expires_at),
                },
                source="agreements_public",
            )

            return JSONResponse(
                {
                    "ok": True,
                    "agreement": {
                        "id": _id_text(locked_agreement.id),
                        "status": locked_agreement.status,
                        "signerRole": "client",
                        "signedAt": None,
                        "clientSignedAt": _iso_datetime(locked_agreement.client_signed_at),
                        "signerName": locked_agreement.signer_name,
                        "signerTitle": locked_agreement.signer_title,
                    },
                    "signedPdfUrl": None,
                    "expiresInSeconds": AGREEMENTS_SIGNED_URL_TTL_SECONDS,
                }
            )

        signature_path = f"agreements/{agreement.id}/signatures/ba.{signature['extension']}"
        signed_pdf_path = f"agreements/{agreement.id}/signed.pdf"
        try:
            upload_agreement_file(signature_path, signature["buffer"], signature["mime"], upsert=True)
        except AgreementServiceError as exc:
            return _agreement_error_response(exc)

        locked_agreement = (
            db.query(ConsultingAgreement)
            .filter(ConsultingAgreement.id == agreement.id)
            .with_for_update()
            .first()
        )
        validation_error = _validate_public_signable_agreement(locked_agreement, signer_role)
        if validation_error:
            db.rollback()
            return validation_error

        try:
            draft_pdf_bytes = download_agreement_file(locked_agreement.draft_pdf_path)
            client_signature_path = (
                _clean_text(getattr(locked_agreement, "client_signature_image_path", None))
                or _clean_text(getattr(locked_agreement, "signature_image_path", None))
            )
            if not client_signature_path:
                db.rollback()
                return _agreement_public_error("client_signature_missing", "Client signature is not available.", status=409)
            client_signature_bytes = download_agreement_file(client_signature_path)
            payload_values = _agreement_template_values_from_row(locked_agreement)
            signed_pdf_bytes = build_signed_agreement_pdf(
                draft_pdf_bytes,
                agreement_id=_id_text(locked_agreement.id) or "",
                payload_values=payload_values,
                client_signer_name=locked_agreement.signer_name or "",
                client_signer_title=locked_agreement.signer_title or "",
                client_signed_at=locked_agreement.client_signed_at or signed_at,
                client_signer_ip=locked_agreement.signer_ip,
                client_signer_user_agent=locked_agreement.signer_user_agent,
                client_authority_confirmed=bool(locked_agreement.signer_authority_confirmed),
                client_accepted=bool(locked_agreement.signer_accepted),
                client_signature={
                    "buffer": client_signature_bytes,
                    "sha256": locked_agreement.client_signature_sha256 or locked_agreement.signature_sha256,
                },
                ba_signer_name=signer_name,
                ba_signer_title=signer_title,
                ba_signer_email=locked_agreement.ba_signer_email or "",
                ba_signed_at=signed_at,
                ba_signer_ip=signer_ip,
                ba_signer_user_agent=signer_user_agent,
                ba_authority_confirmed=True,
                ba_accepted=True,
                ba_signature=signature,
            )
            upload_agreement_file(signed_pdf_path, signed_pdf_bytes, "application/pdf", upsert=True)
        except AgreementServiceError as exc:
            db.rollback()
            return _agreement_error_response(exc)

        previous_current_rows = (
            db.query(ConsultingAgreement)
            .filter(func.lower(ConsultingAgreement.client_email) == locked_agreement.client_email.lower())
            .filter(ConsultingAgreement.document_type == locked_agreement.document_type)
            .filter(ConsultingAgreement.is_current.is_(True))
            .filter(ConsultingAgreement.id != locked_agreement.id)
            .all()
        )
        for previous in previous_current_rows:
            previous.is_current = False
            previous.status = "superseded"
            previous.superseded_at = signed_at
            previous.superseded_by_agreement_id = locked_agreement.id
            previous.updated_at = signed_at

        locked_agreement.status = "signed"
        locked_agreement.is_current = True
        locked_agreement.ba_opened_at = locked_agreement.ba_opened_at or signed_at
        locked_agreement.ba_signer_name = signer_name
        locked_agreement.ba_signer_title = signer_title
        locked_agreement.ba_signer_authority_confirmed = True
        locked_agreement.ba_signer_accepted = True
        locked_agreement.ba_signed_at = signed_at
        locked_agreement.ba_signer_ip = signer_ip
        locked_agreement.ba_signer_user_agent = signer_user_agent
        locked_agreement.ba_signature_image_path = signature_path
        locked_agreement.ba_signature_sha256 = signature["sha256"]
        locked_agreement.signed_at = signed_at
        locked_agreement.signed_pdf_path = signed_pdf_path
        locked_agreement.superseded_at = None
        locked_agreement.superseded_by_agreement_id = None
        locked_agreement.updated_at = signed_at
        snapshot = dict(locked_agreement.template_snapshot or {})
        snapshot["baSignature"] = {
            "signedAt": signed_at.isoformat(),
            "signerName": signer_name,
            "signerTitle": signer_title,
            "signerAccepted": True,
            "signerAuthorityConfirmed": True,
            "signatureSha256": signature["sha256"],
        }
        snapshot["execution"] = {
            "signedAt": signed_at.isoformat(),
            "clientSignedAt": _iso_datetime(locked_agreement.client_signed_at),
            "baSignedAt": signed_at.isoformat(),
            "signaturePage": "workflow_certificate",
        }
        locked_agreement.template_snapshot = snapshot
        db.commit()
        db.refresh(locked_agreement)

        _record_admin_audit_event(
            db,
            request,
            "agreement.ba_signed",
            target_type="consulting_agreement",
            target_id=locked_agreement.id,
            client_email=locked_agreement.client_email,
            metadata={
                "documentType": locked_agreement.document_type,
                "templateVersion": locked_agreement.template_version,
                "signerRole": "ba",
                "baSignerEmail": locked_agreement.ba_signer_email,
                "baSignerTitle": locked_agreement.ba_signer_title,
            },
            source="agreements_public",
        )

        _record_admin_audit_event(
            db,
            request,
            "agreement.signed",
            target_type="consulting_agreement",
            target_id=locked_agreement.id,
            client_email=locked_agreement.client_email,
            metadata={
                "documentType": locked_agreement.document_type,
                "templateVersion": locked_agreement.template_version,
                "signerEmail": locked_agreement.signer_email,
                "baSignerEmail": locked_agreement.ba_signer_email,
                "supersededCount": len(previous_current_rows),
            },
            source="agreements_public",
        )
        if previous_current_rows:
            _record_admin_audit_event(
                db,
                request,
                "agreement.superseded",
                target_type="consulting_agreement",
                target_id=locked_agreement.id,
                client_email=locked_agreement.client_email,
                metadata={
                    "documentType": locked_agreement.document_type,
                    "supersededCount": len(previous_current_rows),
                },
                source="agreements_public",
            )

        signed_url = None
        email_signed_url = None
        try:
            signed_url = create_agreement_signed_url(signed_pdf_path, AGREEMENTS_SIGNED_URL_TTL_SECONDS)
            email_signed_url = create_agreement_signed_url(signed_pdf_path, AGREEMENTS_EMAIL_LINK_TTL_SECONDS)
        except AgreementServiceError:
            logger.exception("[agreements_public] signed url creation failed agreement_id=%s", locked_agreement.id)
        copy_recipients: list[str] = []
        if locked_agreement.signer_email:
            copy_recipients.append(locked_agreement.signer_email)
        company_email = _clean_text(os.getenv("AGREEMENTS_COMPANY_EMAIL"))
        if company_email:
            copy_recipients.append(company_email)
        if locked_agreement.ba_signer_email:
            copy_recipients.append(locked_agreement.ba_signer_email)

        sent_copy_count = 0
        if email_signed_url:
            seen_recipients: set[str] = set()
            for recipient in copy_recipients:
                normalized_recipient = (recipient or "").lower()
                if not normalized_recipient or normalized_recipient in seen_recipients:
                    continue
                seen_recipients.add(normalized_recipient)
                try:
                    email_result = send_agreement_signed_copy_email(
                        recipient,
                        email_signed_url,
                        client_legal_name=locked_agreement.client_legal_name,
                        signed_at=signed_at,
                        company_copy=normalized_recipient == (company_email or "").lower(),
                    )
                    if not (isinstance(email_result, dict) and email_result.get("skipped")):
                        sent_copy_count += 1
                except Exception:
                    logger.exception("[agreements_public] signed copy email failed agreement_id=%s", locked_agreement.id)
        if sent_copy_count:
            _record_admin_audit_event(
                db,
                request,
                "agreement.signed_copy_sent",
                target_type="consulting_agreement",
                target_id=locked_agreement.id,
                client_email=locked_agreement.client_email,
                metadata={
                    "documentType": locked_agreement.document_type,
                    "recipientCount": sent_copy_count,
                },
                source="agreements_public",
            )

        return JSONResponse(
            {
                "ok": True,
                "agreement": {
                    "id": _id_text(locked_agreement.id),
                    "status": locked_agreement.status,
                    "signerRole": "ba",
                    "signedAt": _iso_datetime(locked_agreement.signed_at),
                    "clientSignedAt": _iso_datetime(locked_agreement.client_signed_at),
                    "baSignedAt": _iso_datetime(locked_agreement.ba_signed_at),
                    "signerName": locked_agreement.signer_name,
                    "signerTitle": locked_agreement.signer_title,
                },
                "signedPdfUrl": signed_url,
                "expiresInSeconds": AGREEMENTS_SIGNED_URL_TTL_SECONDS,
            }
        )
    except Exception:
        db.rollback()
        logger.exception("[agreements_public] signing failed")
        return _agreement_public_error("agreement_sign_failed", "Agreement could not be signed.", status=500)
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
def process_admin_financial_analysis_job(
    request: Request,
    job_id: str,
    body: Optional[dict[str, Any]] = Body(default=None),
) -> JSONResponse:
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
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
        if file_extension not in {".csv", ".xlsx", ".pdf"}:
            return _error_response(
                400,
                "unsupported_file_type",
                "Unsupported financial file type.",
            )

        acknowledgment_error = _record_admin_analysis_phi_acknowledgment(
            db,
            request=request,
            body=body,
            job=job,
            job_file=job_file,
            admin_auth_user=admin_auth_user,
            admin_access=admin_access,
        )
        if acknowledgment_error:
            return acknowledgment_error

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
        elif file_extension == ".xlsx":
            data_input = extract_xlsx_text(file_bytes)
            source_format = "xlsx"
        elif file_extension == ".pdf":
            data_input = extract_pdf_text(file_bytes, enable_ocr=True)
            source_format = "pdf"
        else:
            raise AdminFinancialProcessingError(
                "unsupported_file_type",
                "Unsupported financial file type.",
            )
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
        elif file_extension == ".xlsx":
            error_code = "xlsx_extract_failed"
            error_message = "Unable to extract XLSX data."
        elif file_extension == ".pdf":
            error_code = "pdf_extract_failed"
            error_message = "Unable to extract Financial PDF text."
        else:
            error_code = "unsupported_file_type"
            error_message = "Unsupported financial file type."
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
            tool_type="financial",
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
def process_admin_ar_analysis_job(
    request: Request,
    job_id: str,
    body: Optional[dict[str, Any]] = Body(default=None),
) -> JSONResponse:
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
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

        acknowledgment_error = _record_admin_analysis_phi_acknowledgment(
            db,
            request=request,
            body=body,
            job=job,
            job_file=job_file,
            admin_auth_user=admin_auth_user,
            admin_access=admin_access,
        )
        if acknowledgment_error:
            return acknowledgment_error

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
            data_input = extract_pdf_text(file_bytes, enable_ocr=True)
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
            tool_type="ar",
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
def process_admin_claims_analysis_job(
    request: Request,
    job_id: str,
    body: Optional[dict[str, Any]] = Body(default=None),
) -> JSONResponse:
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_ANALYSIS_WRITE)
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

        acknowledgment_error = _record_admin_analysis_phi_acknowledgment(
            db,
            request=request,
            body=body,
            job=job,
            job_file=job_file,
            admin_auth_user=admin_auth_user,
            admin_access=admin_access,
        )
        if acknowledgment_error:
            return acknowledgment_error

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
            data_input = extract_pdf_text(file_bytes, enable_ocr=True)
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
            tool_type="claims",
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
                .filter(Upload.voided_at.is_(None))
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
            .filter(Upload.voided_at.is_(None))
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
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_PDF_GENERATE)
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
        structured_sections,
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
        if _upload_is_voided(upload):
            return _error_response(
                409,
                "voided_upload_cannot_be_used",
                "Voided uploads cannot be used for PDF generation.",
            )

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
    if structured_sections:
        sections["structured"] = structured_sections

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
        _record_admin_audit_event(
            db,
            request,
            "pdf_report.generated",
            target_type="upload",
            target_id=upload_id,
            client_email=client_email,
            metadata={"pdfVersion": next_version},
            admin_auth_user=admin_auth_user,
            admin_access=admin_access,
        )
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


@app.post("/api/admin/secure-uploads/files/{file_id}/download-url")
def create_admin_secure_upload_download_url(request: Request, file_id: str) -> JSONResponse:
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_SECURE_UPLOADS_READ)
    if error_response:
        return error_response

    secure_upload_file_id, validation_error = _required_uuid(file_id, "file_id")
    if validation_error:
        return validation_error

    admin_id = _clean_text(admin_auth_user.get("id"))
    admin_email = _clean_text(admin_auth_user.get("email"))

    db = SessionLocal()
    try:
        upload_file = (
            db.query(UploadPortalFile)
            .filter(UploadPortalFile.id == secure_upload_file_id)
            .first()
        )
    except Exception:
        logger.exception(
            "[admin_secure_uploads] download lookup failed admin_id=%s admin_email=%s file_id=%s",
            admin_id,
            admin_email,
            secure_upload_file_id,
        )
        return _error_response(500, "secure_upload_download_lookup_failed", "Unable to load secure upload file.")
    finally:
        db.close()

    if not upload_file:
        logger.info(
            "[admin_secure_uploads] download_url_result admin_id=%s admin_email=%s file_id=%s status=not_found",
            admin_id,
            admin_email,
            secure_upload_file_id,
        )
        return _error_response(404, "secure_upload_file_not_found", "Secure upload file was not found.")

    if not getattr(upload_file, "completed_at", None):
        logger.info(
            "[admin_secure_uploads] download_url_result admin_id=%s admin_email=%s file_id=%s status=incomplete",
            admin_id,
            admin_email,
            secure_upload_file_id,
        )
        return _error_response(400, "secure_upload_incomplete", "Secure upload file is not completed yet.")

    bucket = _clean_text(getattr(upload_file, "gcs_bucket", None))
    object_name = _clean_text(getattr(upload_file, "object_name", None))
    if not bucket or not object_name:
        logger.warning(
            "[admin_secure_uploads] download_url_result admin_id=%s admin_email=%s file_id=%s status=missing_storage_location",
            admin_id,
            admin_email,
            secure_upload_file_id,
        )
        return _error_response(
            400,
            "secure_upload_storage_location_missing",
            "Secure upload file storage location is missing.",
        )

    configured_bucket = _clean_text(os.getenv("GCS_BUCKET_NAME"))
    if configured_bucket and bucket != configured_bucket:
        logger.warning(
            "[admin_secure_uploads] download_url_result admin_id=%s admin_email=%s file_id=%s status=bucket_mismatch",
            admin_id,
            admin_email,
            secure_upload_file_id,
        )
        return _error_response(
            403,
            "secure_upload_bucket_mismatch",
            "Secure upload file is not in the configured storage bucket.",
        )

    signer_base_url = _clean_text(os.getenv("PORTAL_SIGNER_SERVICE_URL"))
    signer_api_key = _clean_text(os.getenv("PORTAL_SIGNER_API_KEY"))
    if not signer_base_url or not signer_api_key:
        logger.warning(
            "[admin_secure_uploads] download_url_result admin_id=%s admin_email=%s file_id=%s status=signer_config_missing",
            admin_id,
            admin_email,
            secure_upload_file_id,
        )
        return _error_response(
            500,
            "secure_upload_download_not_configured",
            "Secure upload download signing is not configured.",
        )

    content_type = _secure_upload_download_content_type(getattr(upload_file, "content_type", None))
    file_name = _secure_upload_download_filename(getattr(upload_file, "original_filename", None))
    try:
        signer_response = requests.post(
            f"{signer_base_url.rstrip('/')}/signed-download-url",
            headers={"Authorization": f"Bearer {signer_api_key}"},
            json={
                "object_name": object_name,
                "content_type": content_type,
                "filename": file_name,
            },
            timeout=15,
        )
    except requests.RequestException:
        logger.warning(
            "[admin_secure_uploads] download_url_result admin_id=%s admin_email=%s file_id=%s status=signer_request_failed",
            admin_id,
            admin_email,
            secure_upload_file_id,
        )
        return _error_response(502, "secure_upload_download_signer_failed", "Unable to create download link.")

    if signer_response.status_code >= 300:
        logger.warning(
            "[admin_secure_uploads] download_url_result admin_id=%s admin_email=%s file_id=%s status=signer_rejected signer_status=%s",
            admin_id,
            admin_email,
            secure_upload_file_id,
            signer_response.status_code,
        )
        return _error_response(502, "secure_upload_download_signer_failed", "Unable to create download link.")

    try:
        signer_payload = signer_response.json()
    except ValueError:
        logger.warning(
            "[admin_secure_uploads] download_url_result admin_id=%s admin_email=%s file_id=%s status=signer_invalid_json",
            admin_id,
            admin_email,
            secure_upload_file_id,
        )
        return _error_response(502, "secure_upload_download_signer_failed", "Unable to create download link.")

    signed_url = _clean_text(signer_payload.get("signed_url") if isinstance(signer_payload, dict) else None)
    expires_in_seconds = _positive_int(
        signer_payload.get("expires_in_seconds") if isinstance(signer_payload, dict) else None
    )
    if not signed_url or not expires_in_seconds:
        logger.warning(
            "[admin_secure_uploads] download_url_result admin_id=%s admin_email=%s file_id=%s status=signer_response_incomplete",
            admin_id,
            admin_email,
            secure_upload_file_id,
        )
        return _error_response(502, "secure_upload_download_signer_failed", "Unable to create download link.")

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
    logger.info(
        "[admin_secure_uploads] download_url_result admin_id=%s admin_email=%s file_id=%s status=signed expires_in_seconds=%s",
        admin_id,
        admin_email,
        secure_upload_file_id,
        expires_in_seconds,
    )
    _record_admin_audit_event(
        None,
        request,
        "secure_upload.download_url_created",
        target_type="secure_upload_file",
        target_id=secure_upload_file_id,
        client_email=_clean_text(getattr(upload_file, "user_email", None)),
        metadata={
            "contentType": content_type,
            "byteSize": getattr(upload_file, "byte_size", None),
        },
        admin_auth_user=admin_auth_user,
        admin_access=admin_access,
    )
    return JSONResponse(
        {
            "ok": True,
            "downloadUrl": signed_url,
            "expiresInSeconds": expires_in_seconds,
            "expiresAt": _iso_datetime(expires_at),
            "fileName": file_name,
            "contentType": content_type,
        }
    )


@app.post("/api/admin/secure-uploads/requests")
async def create_admin_secure_upload_request(request: Request) -> JSONResponse:
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_SECURE_UPLOADS_WRITE)
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

    _record_admin_audit_event(
        None,
        request,
        "secure_upload.request_sent",
        target_type="secure_upload_request",
        target_id=_clean_text(result.get("request_id")),
        client_email=client_email,
        metadata={"expiresAt": _clean_text(result.get("expires_at"))},
        admin_auth_user=admin_auth_user,
        admin_access=admin_access,
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
    admin_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_BILLING_WRITE)
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
        expires_at = _stripe_timestamp_to_datetime(session_data.get("expires_at"))
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
            expires_at=expires_at,
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
        _record_admin_audit_event(
            db,
            request,
            "checkout_session.created",
            target_type="checkout_session",
            target_id=getattr(local_session, "id", None),
            client_email=client_email,
            metadata={
                "amount": amount,
                "currency": currency,
                "purpose": purpose,
                "uploadCount": len(upload_ids),
                "expiresAt": _iso_datetime(expires_at),
            },
            admin_auth_user=admin_user,
            admin_access=admin_access,
        )
        return JSONResponse(
            {
                "ok": True,
                "checkoutSessionId": checkout_session_id,
                "url": checkout_url,
                "status": _clean_text(session_data.get("status")) or "open",
                "paymentStatus": _clean_text(session_data.get("payment_status")) or "unpaid",
                "expiresAt": _iso_datetime(expires_at),
                "expiredAt": None,
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


@app.post("/api/admin/billing/payment-links")
async def create_admin_offer_payment_link(request: Request) -> JSONResponse:
    admin_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_BILLING_WRITE)
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

    offer_type = (_clean_text(body.get("offerType")) or "").lower()
    billing_mode = (_clean_text(body.get("billingMode")) or _clean_text(body.get("mode")) or "one_time").lower()
    if billing_mode == "recurring":
        return _create_admin_recurring_offer_payment_link(
            request=request,
            body=body,
            client_email=client_email,
            offer_type=offer_type,
            stripe_secret_key=stripe_secret_key,
            admin_user=admin_user,
            admin_access=admin_access,
        )

    if billing_mode != "one_time":
        return _error_response(
            400,
            "invalid_billing_mode",
            "billingMode must be one_time or recurring.",
        )

    offer_config = ADMIN_ONE_TIME_OFFER_PAYMENT_LINKS.get(offer_type)
    if not offer_config:
        return _error_response(
            400,
            "invalid_offer_type",
            "offerType must be one of the supported one-time offers.",
        )

    amount, validation_error = _required_amount(body.get("amount"))
    if validation_error:
        return validation_error

    currency = (_clean_text(body.get("currency")) or "usd").lower()
    if currency != "usd":
        return _error_response(400, "invalid_currency", "currency must be usd.")

    offer_name, validation_error = _optional_limited_text(body.get("offerName"), "offerName", 160)
    if validation_error:
        return validation_error
    offer_name = offer_name or str(offer_config["name"])

    description, validation_error = _optional_limited_text(body.get("description"), "description", 240)
    if validation_error:
        return validation_error
    description = description or offer_name

    internal_note, validation_error = _optional_limited_text(body.get("internalNote"), "internalNote", 2000)
    if validation_error:
        return validation_error

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
            "source": "admin_offer_payment_link",
            "client_email": client_email,
            "offer_type": offer_type,
            "offer_name": offer_name,
            "billing_mode": "one_time",
            "created_by_admin_user_id": str(admin_user.get("id") or ""),
            "upload_count": str(len(upload_ids)),
        }
        if upload_id:
            metadata["upload_id"] = str(upload_id)
        upload_ids_metadata = ",".join(str(selected_upload_id) for selected_upload_id in upload_ids)
        if upload_ids_metadata and len(upload_ids_metadata) <= 500:
            metadata["upload_ids"] = upload_ids_metadata

        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            customer=stripe_customer_id,
            line_items=[
                {
                    "price_data": {
                        "currency": currency,
                        "unit_amount": amount,
                        "product_data": {
                            "name": offer_name,
                            "description": description,
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
        expires_at = _stripe_timestamp_to_datetime(session_data.get("expires_at"))
        if not checkout_session_id or not checkout_url:
            logger.error("[admin_api] Stripe offer payment link missing id or url.")
            db.rollback()
            return _error_response(502, "stripe_checkout_failed", "Unable to create payment link.")

        offer_metadata = {
            "source": "admin_offer_payment_link",
            "offerType": offer_type,
            "offerName": offer_name,
            "billingMode": "one_time",
            "defaultAmount": _optional_int(offer_config.get("default_amount")),
            "description": description,
            "uploadCount": len(upload_ids),
        }
        local_session = StripeCheckoutSession(
            stripe_checkout_session_id=checkout_session_id,
            stripe_customer_id=stripe_customer_id,
            client_email=client_email,
            user_id=getattr(user_record, "id", None),
            upload_id=upload_id,
            purpose=offer_type,
            description=description,
            offer_type=offer_type,
            offer_name=offer_name,
            billing_mode="one_time",
            interval=None,
            internal_note=internal_note,
            offer_metadata=offer_metadata,
            mode=_clean_text(session_data.get("mode")) or "payment",
            status=_clean_text(session_data.get("status")),
            payment_status=_clean_text(session_data.get("payment_status")),
            amount_total=_optional_int(session_data.get("amount_total")) or amount,
            currency=_clean_text(session_data.get("currency")) or currency,
            checkout_url=checkout_url,
            expires_at=expires_at,
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
            "[admin_api] Stripe offer payment link created id=%s client_email=%s offer_type=%s amount=%s currency=%s admin_user_id=%s",
            checkout_session_id,
            client_email,
            offer_type,
            amount,
            currency,
            str(admin_user.get("id") or ""),
        )
        _record_admin_audit_event(
            db,
            request,
            "checkout_session.created",
            target_type="checkout_session",
            target_id=getattr(local_session, "id", None),
            client_email=client_email,
            metadata={
                "amount": amount,
                "currency": currency,
                "offerType": offer_type,
                "offerName": offer_name,
                "billingMode": "one_time",
                "uploadCount": len(upload_ids),
                "expiresAt": _iso_datetime(expires_at),
            },
            admin_auth_user=admin_user,
            admin_access=admin_access,
        )
        return JSONResponse(
            {
                "ok": True,
                "checkoutSessionId": checkout_session_id,
                "url": checkout_url,
                "status": _clean_text(session_data.get("status")) or "open",
                "paymentStatus": _clean_text(session_data.get("payment_status")) or "unpaid",
                "expiresAt": _iso_datetime(expires_at),
                "expiredAt": None,
                "uploadId": _id_text(upload_id),
                "uploadIds": [_id_text(selected_upload_id) for selected_upload_id in upload_ids],
                "relatedUploads": [_upload_payload(upload) for upload in upload_records],
                "offerType": offer_type,
                "offerName": offer_name,
                "billingMode": "one_time",
                "interval": None,
                "internalNote": internal_note,
            }
        )
    except stripe.error.StripeError:
        db.rollback()
        logger.exception(
            "[admin_api] Stripe offer payment link creation failed client_email=%s offer_type=%s amount=%s currency=%s",
            client_email,
            offer_type,
            amount,
            currency,
        )
        return _error_response(502, "stripe_checkout_failed", "Unable to create payment link.")
    except Exception:
        db.rollback()
        logger.exception(
            "[admin_api] offer payment link failed client_email=%s offer_type=%s",
            client_email,
            offer_type,
        )
        return _error_response(500, "payment_link_failed", "Unable to create payment link.")
    finally:
        db.close()


def _create_admin_recurring_offer_payment_link(
    *,
    request: Request,
    body: dict[str, Any],
    client_email: str,
    offer_type: str,
    stripe_secret_key: str,
    admin_user: dict[str, Any],
    admin_access: Optional[AdminUser],
) -> JSONResponse:
    offer_config = ADMIN_RECURRING_OFFER_PAYMENT_LINKS.get(offer_type)
    if not offer_config:
        return _error_response(
            400,
            "invalid_offer_type",
            "offerType must be operations_intelligence_partner for recurring billing.",
        )

    interval = (_clean_text(body.get("interval")) or "month").lower()
    if interval != "month":
        return _error_response(400, "invalid_interval", "interval must be month.")

    amount_source = body.get("amount") if "amount" in body else body.get("monthlyAmount")
    monthly_amount, validation_error = _required_amount(amount_source)
    if validation_error:
        return validation_error

    currency = (_clean_text(body.get("currency")) or "usd").lower()
    if currency != "usd":
        return _error_response(400, "invalid_currency", "currency must be usd.")

    contract_months_source = body.get("contractMonths") if "contractMonths" in body else body.get("contract_months")
    contract_months, validation_error = _required_contract_months(contract_months_source)
    if validation_error:
        return validation_error

    raw_upload_id = _clean_text(body.get("uploadId"))
    raw_upload_ids = body.get("uploadIds")
    if raw_upload_id or (isinstance(raw_upload_ids, list) and raw_upload_ids) or (
        raw_upload_ids is not None and not isinstance(raw_upload_ids, list)
    ):
        return _error_response(
            400,
            "recurring_upload_links_unsupported",
            "Recurring retainer payment links cannot be linked to uploads in this phase.",
        )

    offer_name, validation_error = _optional_limited_text(body.get("offerName"), "offerName", 160)
    if validation_error:
        return validation_error
    offer_name = offer_name or str(offer_config["name"])

    description, validation_error = _optional_limited_text(body.get("description"), "description", 240)
    if validation_error:
        return validation_error
    description = description or offer_name

    internal_note, validation_error = _optional_limited_text(body.get("internalNote"), "internalNote", 2000)
    if validation_error:
        return validation_error

    success_url, validation_error = _required_text(body.get("successUrl"), "successUrl")
    if validation_error:
        return validation_error
    cancel_url, validation_error = _required_text(body.get("cancelUrl"), "cancelUrl")
    if validation_error:
        return validation_error
    if not _is_safe_checkout_url(success_url) or not _is_safe_checkout_url(cancel_url):
        return _error_response(400, "invalid_url", "Checkout URLs must use http or https.")

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
            "source": "admin_offer_payment_link",
            "client_email": client_email,
            "offer_type": offer_type,
            "offer_name": offer_name,
            "billing_mode": "recurring",
            "interval": "month",
            "contract_months": str(contract_months),
            "monthly_amount": str(monthly_amount),
            "created_by_admin_user_id": str(admin_user.get("id") or ""),
        }

        checkout_session = stripe.checkout.Session.create(
            mode="subscription",
            customer=stripe_customer_id,
            line_items=[
                {
                    "price_data": {
                        "currency": currency,
                        "unit_amount": monthly_amount,
                        "recurring": {"interval": "month"},
                        "product_data": {
                            "name": offer_name,
                            "description": description,
                        },
                    },
                    "quantity": 1,
                }
            ],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
            subscription_data={"metadata": metadata},
            api_key=stripe_secret_key,
        )
        session_data = _stripe_object_to_dict(checkout_session)
        checkout_session_id = _clean_text(session_data.get("id"))
        checkout_url = _clean_text(session_data.get("url"))
        stripe_subscription_id = _stripe_object_id(session_data.get("subscription"), "sub_")
        expires_at = _stripe_timestamp_to_datetime(session_data.get("expires_at"))
        if not checkout_session_id or not checkout_url:
            logger.error("[admin_api] Stripe retainer checkout link missing id or url.")
            db.rollback()
            return _error_response(502, "stripe_checkout_failed", "Unable to create subscription link.")

        offer_metadata = {
            "source": "admin_offer_payment_link",
            "offerType": offer_type,
            "offerName": offer_name,
            "billingMode": "recurring",
            "interval": "month",
            "contractMonths": contract_months,
            "monthlyAmount": monthly_amount,
            "description": description,
            "cancelScheduleStatus": "pending_checkout_completion",
        }
        local_session = StripeCheckoutSession(
            stripe_checkout_session_id=checkout_session_id,
            stripe_customer_id=stripe_customer_id,
            client_email=client_email,
            user_id=getattr(user_record, "id", None),
            upload_id=None,
            purpose=offer_type,
            description=description,
            offer_type=offer_type,
            offer_name=offer_name,
            billing_mode="recurring",
            interval="month",
            stripe_subscription_id=stripe_subscription_id,
            contract_months=contract_months,
            monthly_amount=monthly_amount,
            subscription_status=None,
            internal_note=internal_note,
            offer_metadata=offer_metadata,
            mode=_clean_text(session_data.get("mode")) or "subscription",
            status=_clean_text(session_data.get("status")),
            payment_status=_clean_text(session_data.get("payment_status")),
            amount_total=_optional_int(session_data.get("amount_total")) or monthly_amount,
            currency=_clean_text(session_data.get("currency")) or currency,
            checkout_url=checkout_url,
            expires_at=expires_at,
            success_url=success_url,
            cancel_url=cancel_url,
            livemode=bool(session_data.get("livemode", livemode)),
        )
        db.add(local_session)
        db.commit()
        logger.info(
            "[admin_api] Stripe retainer subscription link created id=%s client_email=%s amount=%s contract_months=%s admin_user_id=%s",
            checkout_session_id,
            client_email,
            monthly_amount,
            contract_months,
            str(admin_user.get("id") or ""),
        )
        _record_admin_audit_event(
            db,
            request,
            "checkout_session.created",
            target_type="checkout_session",
            target_id=getattr(local_session, "id", None),
            client_email=client_email,
            metadata={
                "amount": monthly_amount,
                "currency": currency,
                "offerType": offer_type,
                "offerName": offer_name,
                "billingMode": "recurring",
                "interval": "month",
                "contractMonths": contract_months,
                "expiresAt": _iso_datetime(expires_at),
            },
            admin_auth_user=admin_user,
            admin_access=admin_access,
        )
        return JSONResponse(
            {
                "ok": True,
                "checkoutSessionId": checkout_session_id,
                "url": checkout_url,
                "status": _clean_text(session_data.get("status")) or "open",
                "paymentStatus": _clean_text(session_data.get("payment_status")) or "unpaid",
                "expiresAt": _iso_datetime(expires_at),
                "expiredAt": None,
                "uploadId": None,
                "uploadIds": [],
                "relatedUploads": [],
                "offerType": offer_type,
                "offerName": offer_name,
                "billingMode": "recurring",
                "interval": "month",
                "internalNote": internal_note,
                "monthlyAmount": monthly_amount,
                "contractMonths": contract_months,
                "stripeSubscriptionId": stripe_subscription_id,
                "subscriptionStatus": None,
                "currentPeriodEnd": None,
                "cancelAt": None,
            }
        )
    except stripe.error.StripeError:
        db.rollback()
        logger.exception(
            "[admin_api] Stripe retainer subscription link creation failed client_email=%s amount=%s contract_months=%s",
            client_email,
            monthly_amount,
            contract_months,
        )
        return _error_response(502, "stripe_checkout_failed", "Unable to create subscription link.")
    except Exception:
        db.rollback()
        logger.exception(
            "[admin_api] retainer subscription link failed client_email=%s offer_type=%s",
            client_email,
            offer_type,
        )
        return _error_response(500, "payment_link_failed", "Unable to create subscription link.")
    finally:
        db.close()


@app.post("/api/admin/billing/checkout-sessions/{session_id}/expire")
async def expire_admin_checkout_session(request: Request, session_id: str) -> JSONResponse:
    admin_auth_user, admin_access, error_response = _require_admin_permission(request, PERMISSION_BILLING_WRITE)
    if error_response:
        return error_response

    session_uuid, validation_error = _required_uuid(session_id, "session_id")
    if validation_error:
        return validation_error

    db = SessionLocal()
    try:
        local_session = db.query(StripeCheckoutSession).filter(StripeCheckoutSession.id == session_uuid).first()
        if not local_session:
            return _error_response(404, "checkout_session_not_found", "Checkout session was not found.")

        stripe_checkout_session_id = _clean_text(getattr(local_session, "stripe_checkout_session_id", None))
        if not stripe_checkout_session_id:
            return _error_response(
                409,
                "checkout_session_not_expirable",
                "Checkout session cannot be expired.",
            )

        if _checkout_session_is_paid_or_complete(local_session):
            return _error_response(
                409,
                "checkout_session_already_paid",
                "Paid checkout sessions cannot be expired.",
            )

        if _checkout_session_is_expired(local_session):
            return _error_response(
                409,
                "checkout_session_already_expired",
                "Checkout session is already expired.",
            )

        if not _checkout_session_is_open_or_unpaid(local_session):
            return _error_response(
                409,
                "checkout_session_not_expirable",
                "Only open unpaid checkout sessions can be expired.",
            )

        stripe_secret_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
        if not stripe_secret_key:
            logger.error("[admin_api] Stripe secret key is not configured.")
            return _error_response(503, "stripe_not_configured", "Stripe is not configured.")

        expired_session = stripe.checkout.Session.expire(
            stripe_checkout_session_id,
            api_key=stripe_secret_key,
        )
        session_data = _stripe_object_to_dict(expired_session)
        now = datetime.now(timezone.utc)
        _apply_checkout_session_data(
            local_session,
            session_data,
            now,
            expired_at=_stripe_timestamp_to_datetime(session_data.get("expired_at")) or now,
        )
        db.commit()
        db.refresh(local_session)
        logger.info(
            "[admin_api] Stripe checkout session expired manually session_id=%s local_session_id=%s",
            stripe_checkout_session_id,
            session_uuid,
        )
        _record_admin_audit_event(
            db,
            request,
            "checkout_session.expired",
            target_type="checkout_session",
            target_id=session_uuid,
            client_email=_clean_text(getattr(local_session, "client_email", None)),
            metadata={
                "status": _clean_text(getattr(local_session, "status", None)),
                "paymentStatus": _clean_text(getattr(local_session, "payment_status", None)),
                "expiresAt": _iso_datetime(getattr(local_session, "expires_at", None)),
                "expiredAt": _iso_datetime(getattr(local_session, "expired_at", None)),
            },
            admin_auth_user=admin_auth_user,
            admin_access=admin_access,
        )
        return JSONResponse({"ok": True, "checkoutSession": _checkout_session_payload(local_session)})
    except stripe.error.InvalidRequestError as exc:
        db.rollback()
        if _stripe_checkout_expire_error_is_not_expirable(exc):
            logger.warning(
                "[admin_api] Stripe checkout session expire rejected local_session_id=%s code=checkout_session_not_expirable",
                session_uuid,
            )
            return _error_response(
                409,
                "checkout_session_not_expirable",
                "This checkout session cannot be expired because it is no longer open.",
            )
        logger.exception("[admin_api] Stripe checkout session expire failed local_session_id=%s", session_uuid)
        return _error_response(502, "stripe_checkout_expire_failed", "Unable to expire checkout session.")
    except stripe.error.StripeError:
        db.rollback()
        logger.exception("[admin_api] Stripe checkout session expire failed local_session_id=%s", session_uuid)
        return _error_response(502, "stripe_checkout_expire_failed", "Unable to expire checkout session.")
    except Exception:
        db.rollback()
        logger.exception("[admin_api] checkout session expire failed local_session_id=%s", session_uuid)
        return _error_response(500, "checkout_session_expire_failed", "Unable to expire checkout session.")
    finally:
        db.close()


@app.get("/api/admin/billing/client")
def get_admin_billing_client(
    request: Request,
    email: Optional[str] = None,
    uploadStatus: str = Query("active"),
) -> JSONResponse:
    _, _, error_response = _require_admin_permission(request, PERMISSION_BILLING_READ)
    if error_response:
        return error_response

    client_email, validation_error = _required_email(email)
    if validation_error:
        return validation_error
    normalized_upload_status = (uploadStatus or "active").strip().lower()
    if normalized_upload_status not in {"active", "voided", "all"}:
        return _error_response(400, "invalid_upload_status", "uploadStatus must be active, voided, or all.")

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
        _recover_missing_recurring_subscription_sessions(db, checkout_sessions)
        subscriptions = (
            db.query(StripeSubscription)
            .filter(func.lower(StripeSubscription.client_email) == client_email)
            .order_by(StripeSubscription.created_at.desc())
            .limit(25)
            .all()
        )
        uploads_query = db.query(Upload).filter(func.lower(Upload.user_email) == client_email)
        uploads_query = _filter_uploads_by_void_status(uploads_query, normalized_upload_status)
        uploads = uploads_query.order_by(Upload.id.desc()).limit(25).all()
        consultant_reviews = _client_consultant_reviews(db, client_email)
        billing_overrides = (
            db.query(BillingOverride)
            .filter(func.lower(BillingOverride.client_email) == client_email)
            .order_by(BillingOverride.created_at.desc())
            .limit(25)
            .all()
        )
        latest_submission = (
            db.query(ClientSubmission)
            .filter(func.lower(ClientSubmission.user_email) == client_email)
            .order_by(ClientSubmission.submitted_at.desc())
            .first()
        )
        recent_submissions = (
            db.query(ClientSubmission)
            .filter(func.lower(ClientSubmission.user_email) == client_email)
            .order_by(ClientSubmission.submitted_at.desc())
            .limit(10)
            .all()
        )
        recent_submission_ids = [
            getattr(submission, "id", None)
            for submission in recent_submissions
            if getattr(submission, "id", None)
        ]
        recent_uploads_by_submission_id: dict[str, Upload] = {}
        if recent_submission_ids:
            recent_uploads = (
                db.query(Upload)
                .filter(Upload.submission_id.in_(recent_submission_ids))
                .order_by(Upload.id.desc())
                .all()
            )
            for upload in recent_uploads:
                submission_id = _id_text(getattr(upload, "submission_id", None))
                if submission_id and submission_id not in recent_uploads_by_submission_id:
                    recent_uploads_by_submission_id[submission_id] = upload
        user_record = (
            db.query(User)
            .filter(func.lower(User.email) == client_email)
            .first()
        )
        client_profile = _client_billing_profile_payload(
            client_email,
            user_record,
            latest_submission,
        )

        paid_sessions = [
            session
            for session in checkout_sessions
            if _checkout_session_is_paid_or_complete(session)
        ]
        expired_sessions = [
            session
            for session in checkout_sessions
            if _checkout_session_is_expired(session)
        ]
        open_sessions = [
            session
            for session in checkout_sessions
            if _checkout_session_is_open_or_unpaid(session)
        ]
        latest_session = checkout_sessions[0] if checkout_sessions else None
        latest_paid_session = paid_sessions[0] if paid_sessions else None
        checkout_related_uploads = _checkout_related_uploads_by_session(
            db,
            checkout_sessions,
            upload_status=normalized_upload_status,
        )
        checkout_subscriptions = _checkout_subscriptions_by_session(db, checkout_sessions)

        return JSONResponse(
            {
                "ok": True,
                "clientEmail": client_email,
                "latestGhlCid": client_profile["latestGhlCid"],
                "clientProfile": client_profile,
                "customer": _stripe_customer_payload(customers[0]) if customers else None,
                "customers": [_stripe_customer_payload(customer) for customer in customers],
                "summary": {
                    "checkoutSessionCount": len(checkout_sessions),
                    "paidCheckoutSessionCount": len(paid_sessions),
                    "openCheckoutSessionCount": len(open_sessions),
                    "expiredCheckoutSessionCount": len(expired_sessions),
                    "subscriptionCount": len(subscriptions),
                    "manualOverrideCount": len(billing_overrides),
                    "latestPaymentStatus": _clean_text(
                        getattr(latest_session, "payment_status", None)
                    ),
                },
                "latestPaidSession": (
                    _checkout_session_payload(
                        latest_paid_session,
                        checkout_related_uploads.get(_id_text(getattr(latest_paid_session, "id", None)) or "", []),
                        checkout_subscriptions.get(_id_text(getattr(latest_paid_session, "id", None)) or ""),
                    ) if latest_paid_session else None
                ),
                "checkoutSessions": [
                    _checkout_session_payload(
                        session,
                        checkout_related_uploads.get(_id_text(getattr(session, "id", None)) or "", []),
                        checkout_subscriptions.get(_id_text(getattr(session, "id", None)) or ""),
                    )
                    for session in checkout_sessions[:25]
                ],
                "recentSubmissions": [
                    _client_recent_submission_payload(
                        submission,
                        recent_uploads_by_submission_id.get(
                            _id_text(getattr(submission, "id", None)) or ""
                        ),
                    )
                    for submission in recent_submissions
                ],
                "consultantReviews": consultant_reviews,
                "uploads": [_upload_payload(upload) for upload in uploads],
                "billingOverrides": [
                    _billing_override_payload(override)
                    for override in billing_overrides
                ],
                "invoices": [],
                "subscriptions": [_stripe_subscription_payload(subscription) for subscription in subscriptions],
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
    if normalized_status == "unpaid":
        normalized_status = "open"
    if normalized_status not in {"open", "paid", "expired", "all"}:
        return _error_response(400, "invalid_status", "status must be open, unpaid, paid, expired, or all.")

    safe_limit = min(limit, 100)
    normalized_search = (search or "").strip()
    search_like = f"%{normalized_search}%" if normalized_search else None

    db = SessionLocal()
    try:
        checkout_query = db.query(StripeCheckoutSession)
        subscription_query = db.query(StripeSubscription)
        override_query = db.query(BillingOverride)
        if search_like:
            checkout_query = checkout_query.filter(
                or_(
                    StripeCheckoutSession.client_email.ilike(search_like),
                    StripeCheckoutSession.purpose.ilike(search_like),
                )
            )
            subscription_query = subscription_query.filter(
                or_(
                    StripeSubscription.client_email.ilike(search_like),
                    StripeSubscription.offer_name.ilike(search_like),
                    StripeSubscription.offer_type.ilike(search_like),
                )
            )
            override_query = override_query.filter(BillingOverride.client_email.ilike(search_like))

        recovery_candidates = (
            checkout_query.filter(_recurring_checkout_filter())
            .filter(StripeCheckoutSession.stripe_checkout_session_id.isnot(None))
            .filter(
                or_(
                    StripeCheckoutSession.stripe_subscription_id.is_(None),
                    StripeCheckoutSession.subscription_status.is_(None),
                    _checkout_open_filter(),
                )
            )
            .order_by(StripeCheckoutSession.created_at.desc())
            .limit(25)
            .all()
        )
        _recover_missing_recurring_subscription_sessions(db, recovery_candidates)

        checkout_session_count = checkout_query.count()
        subscription_count = subscription_query.count()
        paid_checkout_session_count = checkout_query.filter(_checkout_paid_filter()).count()
        expired_checkout_session_count = checkout_query.filter(_checkout_expired_filter()).count()
        open_checkout_filter = _checkout_open_filter()
        open_checkout_session_count = checkout_query.filter(open_checkout_filter).count()
        manual_override_count = override_query.count()
        needs_review_event_count = (
            db.query(StripeEvent)
            .filter(StripeEvent.processing_status == "needs_review")
            .count()
        )

        filtered_checkout_query = checkout_query
        if normalized_status == "paid":
            filtered_checkout_query = filtered_checkout_query.filter(_checkout_paid_filter())
        elif normalized_status == "expired":
            filtered_checkout_query = filtered_checkout_query.filter(_checkout_expired_filter())
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
        checkout_subscriptions = _checkout_subscriptions_by_session(db, checkout_rows)

        return JSONResponse(
            {
                "ok": True,
                "summary": {
                    "checkoutSessionCount": checkout_session_count,
                    "paidCheckoutSessionCount": paid_checkout_session_count,
                    "openCheckoutSessionCount": open_checkout_session_count,
                    "expiredCheckoutSessionCount": expired_checkout_session_count,
                    "subscriptionCount": subscription_count,
                    "manualOverrideCount": manual_override_count,
                    "needsReviewEventCount": needs_review_event_count,
                },
                "checkoutSessions": [
                    _checkout_session_payload(
                        session,
                        checkout_related_uploads.get(_id_text(getattr(session, "id", None)) or "", []),
                        checkout_subscriptions.get(_id_text(getattr(session, "id", None)) or ""),
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
    if event_type in {"customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"}:
        return _process_stripe_subscription_event(db, event_data, now)
    if event_type in {"invoice.payment_succeeded", "invoice.payment_failed"}:
        return _process_stripe_invoice_event(db, event_data, now)
    if event_type not in {"checkout.session.completed", "checkout.session.expired"}:
        return "processed"

    session_data = _stripe_checkout_session_from_event(event_data)
    checkout_session_id = _clean_text(session_data.get("id"))
    if not checkout_session_id:
        logger.warning("[admin_api] Stripe %s missing session id.", event_type)
        return "needs_review"

    local_session = (
        db.query(StripeCheckoutSession)
        .filter(StripeCheckoutSession.stripe_checkout_session_id == checkout_session_id)
        .first()
    )
    if not local_session:
        logger.warning(
            "[admin_api] Stripe checkout session not found for event_type=%s session_id=%s",
            event_type,
            checkout_session_id,
        )
        return "needs_review"

    _apply_checkout_session_data(
        local_session,
        session_data,
        now,
        event_livemode=event_data.get("livemode"),
        expired_at=(
            _stripe_timestamp_to_datetime(event_data.get("created")) or now
            if event_type == "checkout.session.expired"
            else None
        ),
    )
    if event_type == "checkout.session.completed" and _checkout_session_is_paid_or_complete(local_session):
        if _checkout_session_is_recurring(local_session):
            subscription_status = _sync_subscription_from_checkout_session(db, local_session, session_data, event_data, now)
            if subscription_status == "needs_review":
                return "needs_review"
        else:
            _mark_checkout_session_uploads_paid(db, local_session)
    logger.info(
        "[admin_api] Stripe checkout session event processed event_type=%s session_id=%s status=%s payment_status=%s",
        event_type,
        checkout_session_id,
        local_session.status,
        local_session.payment_status,
    )
    return "processed"


def _process_stripe_subscription_event(db: Any, event_data: dict[str, Any], now: datetime) -> str:
    subscription_data = _stripe_event_object_from_event(event_data)
    subscription_id = _stripe_object_id(subscription_data.get("id"), "sub_")
    if not subscription_id:
        logger.warning("[admin_api] Stripe subscription event missing subscription id.")
        return "needs_review"

    subscription = _upsert_stripe_subscription(db, subscription_data, now)
    if subscription:
        _apply_subscription_to_checkout_sessions(db, subscription, now)
    return "processed"


def _process_stripe_invoice_event(db: Any, event_data: dict[str, Any], now: datetime) -> str:
    invoice_data = _stripe_event_object_from_event(event_data)
    subscription_id = _stripe_object_id(invoice_data.get("subscription"), "sub_")
    if not subscription_id:
        parent = invoice_data.get("parent")
        if isinstance(parent, dict):
            subscription_details = parent.get("subscription_details")
            if isinstance(subscription_details, dict):
                subscription_id = _stripe_object_id(subscription_details.get("subscription"), "sub_")
    if not subscription_id:
        return "processed"

    subscription = (
        db.query(StripeSubscription)
        .filter(StripeSubscription.stripe_subscription_id == subscription_id)
        .first()
    )
    if not subscription:
        logger.info(
            "[admin_api] Stripe invoice event for untracked subscription event_type=%s subscription_id=%s",
            _clean_text(event_data.get("type")) or "unknown",
            subscription_id,
        )
        return "processed"

    event_type = _clean_text(event_data.get("type")) or ""
    subscription.latest_invoice_id = _stripe_object_id(invoice_data.get("id"), "in_") or subscription.latest_invoice_id
    subscription.latest_payment_status = "paid" if event_type == "invoice.payment_succeeded" else "failed"
    subscription.updated_at = now
    _apply_subscription_to_checkout_sessions(db, subscription, now)
    return "processed"


def _sync_subscription_from_checkout_session(
    db: Any,
    local_session: StripeCheckoutSession,
    session_data: dict[str, Any],
    event_data: dict[str, Any],
    now: datetime,
) -> str:
    subscription_value = session_data.get("subscription")
    subscription_id = _stripe_object_id(subscription_value, "sub_")
    if not subscription_id:
        _update_checkout_offer_metadata(
            local_session,
            {
                "cancelScheduleStatus": "needs_review",
                "cancelScheduleReason": "missing_subscription_id",
            },
        )
        logger.warning(
            "[admin_api] completed recurring checkout session missing subscription id session_id=%s",
            _clean_text(getattr(local_session, "stripe_checkout_session_id", None)),
        )
        return "needs_review"

    local_session.stripe_subscription_id = subscription_id
    stripe_secret_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    subscription_data = _stripe_expanded_object_to_dict(subscription_value)
    if not subscription_data:
        subscription_data = {"id": subscription_id, "metadata": _stripe_metadata_from_checkout_session(local_session)}
    retrieved_subscription = bool(
        subscription_data.get("current_period_end") and subscription_data.get("status")
    )
    if stripe_secret_key and not retrieved_subscription:
        try:
            subscription_data = _stripe_object_to_dict(
                stripe.Subscription.retrieve(subscription_id, api_key=stripe_secret_key)
            ) or subscription_data
            retrieved_subscription = True
        except stripe.error.StripeError:
            logger.exception(
                "[admin_api] Stripe subscription retrieve failed subscription_id=%s",
                subscription_id,
            )

    local_subscription = _upsert_stripe_subscription(
        db,
        subscription_data,
        now,
        local_session=local_session,
        session_data=session_data,
    )

    if not stripe_secret_key or not retrieved_subscription:
        _update_checkout_offer_metadata(
            local_session,
            {
                "cancelScheduleStatus": "needs_review",
                "cancelScheduleReason": "subscription_retrieve_failed",
            },
        )
        if local_subscription:
            _merge_subscription_metadata(
                local_subscription,
                {
                    "cancelScheduleStatus": "needs_review",
                    "cancelScheduleReason": "subscription_retrieve_failed",
                },
            )
        _apply_subscription_to_checkout_sessions(db, local_subscription, now) if local_subscription else None
        return "needs_review"

    cancel_at = _retainer_cancel_at(subscription_data, local_session, session_data, now)
    if not cancel_at:
        _update_checkout_offer_metadata(
            local_session,
            {
                "cancelScheduleStatus": "needs_review",
                "cancelScheduleReason": "missing_contract_months_or_start",
            },
        )
        if local_subscription:
            _merge_subscription_metadata(
                local_subscription,
                {
                    "cancelScheduleStatus": "needs_review",
                    "cancelScheduleReason": "missing_contract_months_or_start",
                },
            )
        _apply_subscription_to_checkout_sessions(db, local_subscription, now) if local_subscription else None
        return "needs_review"

    try:
        updated_subscription = stripe.Subscription.modify(
            subscription_id,
            cancel_at=int(cancel_at.timestamp()),
            api_key=stripe_secret_key,
        )
        subscription_data = _stripe_object_to_dict(updated_subscription) or subscription_data
        local_subscription = _upsert_stripe_subscription(
            db,
            subscription_data,
            now,
            local_session=local_session,
            session_data=session_data,
            cancel_at_override=cancel_at,
            cancel_schedule_status="scheduled",
        )
        _update_checkout_offer_metadata(
            local_session,
            {
                "cancelScheduleStatus": "scheduled",
                "cancelAt": _iso_datetime(cancel_at),
            },
        )
        _apply_subscription_to_checkout_sessions(db, local_subscription, now) if local_subscription else None
        return "processed"
    except stripe.error.StripeError:
        logger.exception(
            "[admin_api] Stripe subscription cancel_at scheduling failed subscription_id=%s",
            subscription_id,
        )
        if local_subscription:
            local_subscription.cancel_at = cancel_at
            _merge_subscription_metadata(
                local_subscription,
                {
                    "cancelScheduleStatus": "needs_review",
                    "cancelScheduleReason": "stripe_cancel_schedule_failed",
                    "intendedCancelAt": _iso_datetime(cancel_at),
                },
            )
            _apply_subscription_to_checkout_sessions(db, local_subscription, now)
        _update_checkout_offer_metadata(
            local_session,
            {
                "cancelScheduleStatus": "needs_review",
                "cancelScheduleReason": "stripe_cancel_schedule_failed",
                "intendedCancelAt": _iso_datetime(cancel_at),
            },
        )
        return "needs_review"


def _checkout_session_is_paid_or_complete(session: StripeCheckoutSession) -> bool:
    payment_status = (_clean_text(getattr(session, "payment_status", None)) or "").lower()
    status = (_clean_text(getattr(session, "status", None)) or "").lower()
    return payment_status == "paid" or status in {"complete", "completed"}


def _checkout_session_is_expired(session: StripeCheckoutSession) -> bool:
    status = (_clean_text(getattr(session, "status", None)) or "").lower()
    return status == "expired" or bool(getattr(session, "expired_at", None))


def _checkout_session_is_open_or_unpaid(session: StripeCheckoutSession) -> bool:
    return not _checkout_session_is_paid_or_complete(session) and not _checkout_session_is_expired(session)


def _checkout_session_is_recurring(session: StripeCheckoutSession) -> bool:
    billing_mode = (_clean_text(getattr(session, "billing_mode", None)) or "").lower()
    mode = (_clean_text(getattr(session, "mode", None)) or "").lower()
    return billing_mode == "recurring" or mode == "subscription"


def _checkout_session_cancel_schedule_status(session: StripeCheckoutSession) -> str:
    offer_metadata = getattr(session, "offer_metadata", None)
    if isinstance(offer_metadata, dict):
        return (_clean_text(offer_metadata.get("cancelScheduleStatus")) or "").lower()
    return ""


def _checkout_session_needs_subscription_recovery(session: StripeCheckoutSession) -> bool:
    if not _checkout_session_is_recurring(session):
        return False
    if not _clean_text(getattr(session, "stripe_checkout_session_id", None)):
        return False
    if not _clean_text(getattr(session, "stripe_subscription_id", None)):
        return True
    if not _clean_text(getattr(session, "subscription_status", None)):
        return True
    return _checkout_session_cancel_schedule_status(session) == "pending_checkout_completion"


def _stripe_checkout_expire_error_is_not_expirable(error: Exception) -> bool:
    text_parts = [
        str(error),
        _clean_text(getattr(error, "user_message", None)) or "",
        _clean_text(getattr(error, "code", None)) or "",
        _clean_text(getattr(error, "param", None)) or "",
    ]
    json_body = getattr(error, "json_body", None)
    if isinstance(json_body, dict):
        error_body = json_body.get("error")
        if isinstance(error_body, dict):
            text_parts.extend(
                [
                    _clean_text(error_body.get("message")) or "",
                    _clean_text(error_body.get("code")) or "",
                    _clean_text(error_body.get("param")) or "",
                ]
            )

    error_text = " ".join(part for part in text_parts if part).lower()
    if not error_text:
        return False

    return (
        ("expire" in error_text or "expired" in error_text)
        and ("open" in error_text or "not_expirable" in error_text or "not expirable" in error_text)
        and any(
            marker in error_text
            for marker in ("complete", "completed", "paid", "expired", "not open", "no longer open", "status")
        )
    )


def _apply_checkout_session_data(
    local_session: StripeCheckoutSession,
    session_data: dict[str, Any],
    now: datetime,
    *,
    event_livemode: object = None,
    expired_at: Optional[datetime] = None,
) -> None:
    local_session.status = _clean_text(session_data.get("status")) or local_session.status
    local_session.payment_status = (
        _clean_text(session_data.get("payment_status")) or local_session.payment_status
    )
    amount_total = _optional_int(session_data.get("amount_total"))
    if amount_total is not None:
        local_session.amount_total = amount_total
    local_session.currency = _clean_text(session_data.get("currency")) or local_session.currency
    local_session.stripe_customer_id = (
        _stripe_object_id(session_data.get("customer"), "cus_") or local_session.stripe_customer_id
    )
    local_session.stripe_subscription_id = (
        _stripe_object_id(session_data.get("subscription"), "sub_") or local_session.stripe_subscription_id
    )
    session_livemode = session_data.get("livemode", event_livemode)
    if session_livemode is not None:
        local_session.livemode = bool(session_livemode)
    expires_at = _stripe_timestamp_to_datetime(session_data.get("expires_at"))
    if expires_at is not None:
        local_session.expires_at = expires_at
    if expired_at is not None:
        local_session.expired_at = expired_at
    elif _checkout_session_is_expired(local_session) and not getattr(local_session, "expired_at", None):
        local_session.expired_at = now
    local_session.updated_at = now


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


def _stripe_object_id(value: Any, expected_prefix: Optional[str] = None) -> Optional[str]:
    candidate: Optional[str] = None
    if isinstance(value, str):
        candidate = _clean_text(value)
    elif isinstance(value, dict):
        raw_id = value.get("id")
        if isinstance(raw_id, str):
            candidate = _clean_text(raw_id)
    else:
        raw_id = getattr(value, "id", None)
        if isinstance(raw_id, str):
            candidate = _clean_text(raw_id)

    if not candidate:
        return None
    if expected_prefix and not candidate.startswith(expected_prefix):
        return None
    return candidate


def _stripe_expanded_object_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {}
    if isinstance(value, dict):
        return value
    return _stripe_object_to_dict(value)


def _stripe_id(value: object) -> Optional[str]:
    return _stripe_object_id(value)


def _stripe_event_object_from_event(event_data: dict[str, Any]) -> dict[str, Any]:
    data = event_data.get("data")
    if not isinstance(data, dict):
        return {}
    stripe_object = data.get("object")
    if isinstance(stripe_object, dict):
        return stripe_object
    return _stripe_object_to_dict(stripe_object)


def _stripe_metadata_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    metadata: dict[str, str] = {}
    for key, item in value.items():
        safe_key = _clean_text(key)
        safe_value = _clean_text(item)
        if safe_key and safe_value is not None:
            metadata[safe_key] = safe_value
    return metadata


def _stripe_metadata_from_checkout_session(session: StripeCheckoutSession) -> dict[str, str]:
    metadata = {
        "source": "admin_offer_payment_link",
        "client_email": _clean_text(getattr(session, "client_email", None)) or "",
        "offer_type": _clean_text(getattr(session, "offer_type", None)) or "",
        "offer_name": _clean_text(getattr(session, "offer_name", None)) or "",
        "billing_mode": _clean_text(getattr(session, "billing_mode", None)) or "recurring",
        "interval": _clean_text(getattr(session, "interval", None)) or "month",
        "contract_months": str(_optional_int(getattr(session, "contract_months", None)) or ""),
        "monthly_amount": str(_optional_int(getattr(session, "monthly_amount", None)) or ""),
    }
    return {key: value for key, value in metadata.items() if value}


def _metadata_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = _clean_text(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _subscription_items(subscription_data: dict[str, Any]) -> list[dict[str, Any]]:
    items = subscription_data.get("items")
    if not isinstance(items, dict):
        return []
    data = items.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _subscription_price_amount(subscription_data: dict[str, Any]) -> Optional[int]:
    for item in _subscription_items(subscription_data):
        price = item.get("price")
        if isinstance(price, dict):
            amount = _optional_int(price.get("unit_amount"))
            if amount is not None:
                return amount
    return None


def _subscription_currency(subscription_data: dict[str, Any]) -> Optional[str]:
    for item in _subscription_items(subscription_data):
        price = item.get("price")
        if isinstance(price, dict):
            currency = _clean_text(price.get("currency"))
            if currency:
                return currency
    return None


def _subscription_latest_invoice_payment_status(subscription_data: dict[str, Any]) -> str:
    latest_invoice = subscription_data.get("latest_invoice")
    if isinstance(latest_invoice, dict):
        invoice_status = (_clean_text(latest_invoice.get("status")) or "").lower()
        if invoice_status:
            return invoice_status
        payment_intent = latest_invoice.get("payment_intent")
        if isinstance(payment_intent, dict):
            payment_intent_status = (_clean_text(payment_intent.get("status")) or "").lower()
            if payment_intent_status == "succeeded":
                return "paid"
            if payment_intent_status:
                return payment_intent_status
    return ""


def _subscription_period_datetime(subscription_data: dict[str, Any], field_name: str) -> Optional[datetime]:
    value = _stripe_timestamp_to_datetime(subscription_data.get(field_name))
    if value:
        return value
    for item in _subscription_items(subscription_data):
        value = _stripe_timestamp_to_datetime(item.get(field_name))
        if value:
            return value
    return None


def _upsert_stripe_subscription(
    db: Any,
    subscription_data: dict[str, Any],
    now: datetime,
    *,
    local_session: Optional[StripeCheckoutSession] = None,
    session_data: Optional[dict[str, Any]] = None,
    cancel_at_override: Optional[datetime] = None,
    cancel_schedule_status: Optional[str] = None,
) -> Optional[StripeSubscription]:
    subscription_id = _stripe_object_id(subscription_data.get("id"), "sub_")
    if not subscription_id:
        return None

    metadata = _stripe_metadata_dict(subscription_data.get("metadata"))
    existing = (
        db.query(StripeSubscription)
        .filter(StripeSubscription.stripe_subscription_id == subscription_id)
        .first()
    )
    client_email = (
        metadata.get("client_email")
        or _clean_text(getattr(local_session, "client_email", None))
        or _clean_text(getattr(existing, "client_email", None))
    )
    if not client_email:
        return None

    contract_months = (
        _metadata_int(metadata.get("contract_months"))
        or _optional_int(getattr(local_session, "contract_months", None))
        or _optional_int(getattr(existing, "contract_months", None))
    )
    monthly_amount = (
        _metadata_int(metadata.get("monthly_amount"))
        or _optional_int(getattr(local_session, "monthly_amount", None))
        or _subscription_price_amount(subscription_data)
        or _optional_int(getattr(existing, "monthly_amount", None))
    )
    checkout_session_id = _clean_text(getattr(local_session, "stripe_checkout_session_id", None))
    session_payload = session_data or {}
    stripe_customer_id = (
        _stripe_object_id(subscription_data.get("customer"), "cus_")
        or _stripe_object_id(session_payload.get("customer"), "cus_")
        or _clean_text(getattr(local_session, "stripe_customer_id", None))
        or _clean_text(getattr(existing, "stripe_customer_id", None))
    )
    latest_invoice_id = _stripe_object_id(subscription_data.get("latest_invoice"), "in_")
    invoice_payment_status = _subscription_latest_invoice_payment_status(subscription_data)
    local_payment_status = _clean_text(getattr(local_session, "payment_status", None))
    latest_payment_status = (
        ("paid" if invoice_payment_status == "paid" else "")
        or local_payment_status
        or invoice_payment_status
        or _clean_text(getattr(existing, "latest_payment_status", None))
    )
    cancel_at = cancel_at_override or _stripe_timestamp_to_datetime(subscription_data.get("cancel_at"))
    if existing:
        subscription = existing
    else:
        subscription = StripeSubscription(
            stripe_subscription_id=subscription_id,
            client_email=client_email,
            created_at=now,
        )
        db.add(subscription)

    subscription.client_email = client_email
    subscription.user_id = getattr(local_session, "user_id", None) or getattr(subscription, "user_id", None)
    subscription.stripe_customer_id = stripe_customer_id
    subscription.source_checkout_session_id = getattr(local_session, "id", None) or getattr(subscription, "source_checkout_session_id", None)
    subscription.stripe_checkout_session_id = checkout_session_id or getattr(subscription, "stripe_checkout_session_id", None)
    subscription.offer_type = (
        metadata.get("offer_type")
        or _clean_text(getattr(local_session, "offer_type", None))
        or _clean_text(getattr(subscription, "offer_type", None))
    )
    subscription.offer_name = (
        metadata.get("offer_name")
        or _clean_text(getattr(local_session, "offer_name", None))
        or _clean_text(getattr(subscription, "offer_name", None))
    )
    subscription.billing_mode = metadata.get("billing_mode") or "recurring"
    subscription.interval = metadata.get("interval") or _clean_text(getattr(local_session, "interval", None)) or "month"
    subscription.monthly_amount = monthly_amount
    subscription.currency = _clean_text(subscription_data.get("currency")) or _subscription_currency(subscription_data) or _clean_text(getattr(local_session, "currency", None)) or "usd"
    subscription.contract_months = contract_months
    subscription.status = _clean_text(subscription_data.get("status")) or getattr(subscription, "status", None)
    subscription.current_period_start = _subscription_period_datetime(subscription_data, "current_period_start") or getattr(subscription, "current_period_start", None)
    subscription.current_period_end = _subscription_period_datetime(subscription_data, "current_period_end") or getattr(subscription, "current_period_end", None)
    subscription.cancel_at = cancel_at or getattr(subscription, "cancel_at", None)
    if isinstance(subscription_data.get("cancel_at_period_end"), bool):
        subscription.cancel_at_period_end = subscription_data.get("cancel_at_period_end")
    canceled_at = _stripe_timestamp_to_datetime(subscription_data.get("canceled_at"))
    if canceled_at:
        subscription.canceled_at = canceled_at
    subscription.latest_invoice_id = latest_invoice_id or getattr(subscription, "latest_invoice_id", None)
    subscription.latest_payment_status = latest_payment_status
    subscription.internal_note = _clean_text(getattr(local_session, "internal_note", None)) or getattr(subscription, "internal_note", None)
    subscription.livemode = bool(subscription_data.get("livemode", getattr(local_session, "livemode", False)))
    subscription.updated_at = now
    if metadata:
        _merge_subscription_metadata(subscription, metadata)
    if cancel_schedule_status:
        _merge_subscription_metadata(subscription, {"cancelScheduleStatus": cancel_schedule_status})
    return subscription


def _merge_subscription_metadata(subscription: StripeSubscription, metadata: dict[str, Any]) -> None:
    current = getattr(subscription, "subscription_metadata", None)
    if not isinstance(current, dict):
        current = {}
    merged = dict(current)
    for key, value in metadata.items():
        safe_key = _clean_text(key)
        if not safe_key:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            merged[safe_key] = value
    subscription.subscription_metadata = merged


def _update_checkout_offer_metadata(session: StripeCheckoutSession, metadata: dict[str, Any]) -> None:
    current = getattr(session, "offer_metadata", None)
    if not isinstance(current, dict):
        current = {}
    updated = dict(current)
    for key, value in metadata.items():
        safe_key = _clean_text(key)
        if safe_key:
            updated[safe_key] = value
    session.offer_metadata = updated


def _apply_subscription_to_checkout_sessions(
    db: Any,
    subscription: Optional[StripeSubscription],
    now: datetime,
) -> None:
    if not subscription:
        return
    subscription_id = _clean_text(getattr(subscription, "stripe_subscription_id", None))
    if not subscription_id:
        return

    filters = [StripeCheckoutSession.stripe_subscription_id == subscription_id]
    stripe_checkout_session_id = _clean_text(getattr(subscription, "stripe_checkout_session_id", None))
    if stripe_checkout_session_id:
        filters.append(StripeCheckoutSession.stripe_checkout_session_id == stripe_checkout_session_id)
    rows = db.query(StripeCheckoutSession).filter(or_(*filters)).all()
    for session in rows:
        session.stripe_subscription_id = subscription_id
        session.subscription_status = _clean_text(getattr(subscription, "status", None))
        latest_payment_status = _clean_text(getattr(subscription, "latest_payment_status", None))
        if latest_payment_status:
            session.payment_status = latest_payment_status
        session.current_period_end = getattr(subscription, "current_period_end", None)
        session.cancel_at = getattr(subscription, "cancel_at", None)
        session.contract_months = (
            _optional_int(getattr(session, "contract_months", None))
            or _optional_int(getattr(subscription, "contract_months", None))
        )
        session.monthly_amount = (
            _optional_int(getattr(session, "monthly_amount", None))
            or _optional_int(getattr(subscription, "monthly_amount", None))
        )
        session.updated_at = now


def _retainer_cancel_at(
    subscription_data: dict[str, Any],
    local_session: StripeCheckoutSession,
    session_data: dict[str, Any],
    now: datetime,
) -> Optional[datetime]:
    contract_months = _optional_int(getattr(local_session, "contract_months", None))
    if not contract_months:
        metadata = _stripe_metadata_dict(subscription_data.get("metadata"))
        contract_months = _metadata_int(metadata.get("contract_months"))
    if not contract_months:
        return None

    start_at = (
        _subscription_period_datetime(subscription_data, "current_period_start")
        or _stripe_timestamp_to_datetime(subscription_data.get("start_date"))
        or _stripe_timestamp_to_datetime(session_data.get("created"))
        or now
    )
    return _add_months(start_at, contract_months)


def _add_months(value: datetime, months: int) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


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


def _stripe_subscription_payload(subscription: StripeSubscription) -> dict[str, Any]:
    return {
        "id": _id_text(getattr(subscription, "id", None)),
        "clientEmail": _clean_text(getattr(subscription, "client_email", None)),
        "stripeCustomerId": _clean_text(getattr(subscription, "stripe_customer_id", None)),
        "stripeSubscriptionId": _clean_text(getattr(subscription, "stripe_subscription_id", None)),
        "sourceCheckoutSessionId": _id_text(getattr(subscription, "source_checkout_session_id", None)),
        "stripeCheckoutSessionId": _clean_text(getattr(subscription, "stripe_checkout_session_id", None)),
        "offerType": _clean_text(getattr(subscription, "offer_type", None)),
        "offerName": _clean_text(getattr(subscription, "offer_name", None)),
        "billingMode": _clean_text(getattr(subscription, "billing_mode", None)),
        "interval": _clean_text(getattr(subscription, "interval", None)),
        "monthlyAmount": _optional_int(getattr(subscription, "monthly_amount", None)),
        "currency": _clean_text(getattr(subscription, "currency", None)),
        "contractMonths": _optional_int(getattr(subscription, "contract_months", None)),
        "status": _clean_text(getattr(subscription, "status", None)),
        "currentPeriodStart": _iso_datetime(getattr(subscription, "current_period_start", None)),
        "currentPeriodEnd": _iso_datetime(getattr(subscription, "current_period_end", None)),
        "cancelAt": _iso_datetime(getattr(subscription, "cancel_at", None)),
        "cancelAtPeriodEnd": bool(getattr(subscription, "cancel_at_period_end", False)),
        "canceledAt": _iso_datetime(getattr(subscription, "canceled_at", None)),
        "latestInvoiceId": _clean_text(getattr(subscription, "latest_invoice_id", None)),
        "latestPaymentStatus": _clean_text(getattr(subscription, "latest_payment_status", None)),
        "internalNote": _clean_text(getattr(subscription, "internal_note", None)),
        "livemode": bool(getattr(subscription, "livemode", False)),
        "createdAt": _iso_datetime(getattr(subscription, "created_at", None)),
        "updatedAt": _iso_datetime(getattr(subscription, "updated_at", None)),
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


def _legacy_analysis_payload_with_structured(analysis_data: dict[str, Any]) -> str:
    payload = {
        "raw_analyses": analysis_data["raw_analyses"],
        "deduplicated_issues": analysis_data["deduplicated_issues"],
        "total_issue_count": analysis_data["total_issue_count"],
        "all_trends": analysis_data.get("all_trends", []),
    }
    structured_analysis = analysis_data.get("structured_analysis")
    if isinstance(structured_analysis, dict):
        payload["structured_analysis"] = structured_analysis
    provider_structured_outputs = analysis_data.get("provider_structured_outputs")
    if isinstance(provider_structured_outputs, dict):
        payload["provider_structured_outputs"] = provider_structured_outputs
    structured_provider_statuses = analysis_data.get("structured_provider_statuses")
    if isinstance(structured_provider_statuses, dict):
        payload["structured_provider_statuses"] = structured_provider_statuses
    return json.dumps(payload)


def _legacy_financial_analysis_payload(analysis_data: dict[str, Any]) -> str:
    return _legacy_analysis_payload_with_structured(analysis_data)


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
    return _legacy_analysis_payload_with_structured(analysis_data)


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
    return _legacy_analysis_payload_with_structured(analysis_data)


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
        "voided": _upload_is_voided(upload),
        "voidedAt": _iso_datetime(getattr(upload, "voided_at", None)),
        "voidReason": _clean_text(getattr(upload, "void_reason", None)),
        "voidedByAdminEmail": _clean_text(getattr(upload, "voided_by_admin_email", None)),
        "analysis": {
            "hasAnalysisData": analysis_payload is not None,
            "opportunities": _pdf_generator_opportunities(analysis_payload or {}),
            "trends": _pdf_generator_trends(analysis_payload or {}),
            "keyTrends": _pdf_generator_key_trends(analysis_payload or {}),
            "structured": _pdf_generator_structured_draft(analysis_payload or {}),
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


PDF_GENERATOR_STRUCTURED_MAX_FINDINGS = 10
PDF_GENERATOR_STRUCTURED_MAX_EVIDENCE = 3
PDF_GENERATOR_STRUCTURED_MAX_LIST_ITEMS = 10
PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS = 240
PDF_GENERATOR_STRUCTURED_TEXT_CHARS = 1600
PDF_GENERATOR_STRUCTURED_SENSITIVE_MARKERS = (
    "signed_url",
    "signed url",
    "signedurl",
    "checkout_url",
    "checkout url",
    "token",
    "secret",
    "api_key",
    "api key",
    "password",
    "storage/v1/object",
    "object_name",
    "object name",
    "gcs_path",
    "gcs path",
    "gs://",
    "http://",
    "https://",
)


def _pdf_generator_structured_draft(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    structured = payload.get("structured_analysis")
    if not isinstance(structured, dict):
        return None

    executive_summary = _pdf_generator_structured_executive_summary(
        structured.get("executiveSummary")
    )
    ranked_findings = _pdf_generator_structured_findings(
        structured.get("rankedFindings")
    )
    data_quality_notes = _pdf_generator_structured_text_list(
        structured.get("dataQualityNotes")
    )
    implementation_priorities = _pdf_generator_structured_text_list(
        structured.get("implementationPriorities")
    )
    suggested_report_sections = _pdf_generator_structured_text_list(
        structured.get("suggestedReportSections")
    )

    has_content = bool(
        any(executive_summary.values())
        or ranked_findings
        or data_quality_notes
        or implementation_priorities
        or suggested_report_sections
    )
    if not has_content:
        return None

    return {
        "available": True,
        "schemaVersion": _pdf_generator_structured_text(
            structured.get("schemaVersion"),
            max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
        ),
        "toolType": _pdf_generator_structured_text(
            structured.get("toolType"),
            max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
        ),
        "generatedAt": _pdf_generator_structured_text(
            payload.get("generatedAt"),
            max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
        ),
        "sourceFormat": _pdf_generator_structured_text(
            payload.get("sourceFormat"),
            max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
        ),
        "executiveSummary": executive_summary,
        "rankedFindings": ranked_findings,
        "dataQualityNotes": data_quality_notes,
        "implementationPriorities": implementation_priorities,
        "suggestedReportSections": suggested_report_sections,
    }


def _pdf_generator_structured_executive_summary(value: object) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    return {
        "summary": _pdf_generator_structured_text(source.get("summary")),
        "primaryConcern": _pdf_generator_structured_text(source.get("primaryConcern")),
        "recommendedFocus": _pdf_generator_structured_text(source.get("recommendedFocus")),
    }


def _pdf_generator_structured_findings(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    findings: list[dict[str, Any]] = []
    for index, item in enumerate(value[:PDF_GENERATOR_STRUCTURED_MAX_FINDINGS], start=1):
        if not isinstance(item, dict):
            continue

        rank = _pdf_generator_structured_rank(item.get("rank"), index)
        evidence = _pdf_generator_structured_evidence(item.get("evidence"))
        finding = {
            "id": f"finding-{index}",
            "rank": rank,
            "title": _pdf_generator_structured_text(
                item.get("title"),
                max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
            ),
            "category": _pdf_generator_structured_text(
                item.get("category"),
                max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
            ),
            "severity": _pdf_generator_structured_text(
                item.get("severity"),
                max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
            ),
            "confidence": _pdf_generator_structured_text(
                item.get("confidence"),
                max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
            ),
            "financialValue": _pdf_generator_structured_text(
                item.get("financialValue"),
                max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
            ),
            "evidence": evidence,
            "operationalImplication": _pdf_generator_structured_text(
                item.get("operationalImplication")
            ),
            "recommendedAction": _pdf_generator_structured_text(
                item.get("recommendedAction")
            ),
            "followUpQuestion": _pdf_generator_structured_text(
                item.get("followUpQuestion")
            ),
            "estimatedImpactCategory": _pdf_generator_structured_text(
                item.get("estimatedImpactCategory"),
                max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
            ),
            "implementationDifficulty": _pdf_generator_structured_text(
                item.get("implementationDifficulty"),
                max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
            ),
            "clientFacingSummary": _pdf_generator_structured_text(
                item.get("clientFacingSummary")
            ),
        }
        if any(
            (
                finding["title"],
                finding["financialValue"],
                finding["operationalImplication"],
                finding["recommendedAction"],
                finding["clientFacingSummary"],
                evidence,
            )
        ):
            findings.append(finding)
    return findings


def _pdf_generator_structured_evidence(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    evidence_items: list[dict[str, str]] = []
    for item in value[:PDF_GENERATOR_STRUCTURED_MAX_EVIDENCE]:
        if not isinstance(item, dict):
            continue
        evidence = {
            "label": _pdf_generator_structured_text(
                item.get("label"),
                max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
            ),
            "value": _pdf_generator_structured_text(
                item.get("value"),
                max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
            ),
            "sourceHint": _pdf_generator_structured_text(
                item.get("sourceHint"),
                max_chars=PDF_GENERATOR_STRUCTURED_SHORT_TEXT_CHARS,
            ),
        }
        if any(evidence.values()):
            evidence_items.append(evidence)
    return evidence_items


def _pdf_generator_structured_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []

    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _pdf_generator_structured_text(item)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(text)
        if len(items) >= PDF_GENERATOR_STRUCTURED_MAX_LIST_ITEMS:
            break
    return items


def _pdf_generator_structured_rank(value: object, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int) and value > 0:
        return value
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _pdf_generator_structured_text(
    value: object,
    *,
    max_chars: int = PDF_GENERATOR_STRUCTURED_TEXT_CHARS,
) -> str:
    if value is None or isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        return ""

    text = _clean_text(value) or ""
    if not text:
        return ""

    lowered = text.lower()
    if any(marker in lowered for marker in PDF_GENERATOR_STRUCTURED_SENSITIVE_MARKERS):
        return ""

    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "..."
    return text


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
) -> tuple[UUID, list[dict[str, str]], list[str], list[str], dict[str, Any], str, Optional[JSONResponse]]:
    upload_id_text = _clean_text(body.get("uploadId"))
    if not upload_id_text:
        return UUID(int=0), [], [], [], {}, "", _error_response(400, "missing_upload_id", "uploadId is required.")
    try:
        upload_id = UUID(upload_id_text)
    except ValueError:
        return UUID(int=0), [], [], [], {}, "", _error_response(400, "invalid_upload_id", "uploadId must be a valid UUID.")

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
        return UUID(int=0), [], [], [], {}, "", notes_error

    opportunities, opportunities_error = _pdf_generation_opportunities(opportunities_value)
    if opportunities_error:
        return UUID(int=0), [], [], [], {}, "", opportunities_error

    trends, trends_error = _pdf_generation_text_list(
        trends_value,
        "trends",
        max_items=20,
        max_length=4000,
    )
    if trends_error:
        return UUID(int=0), [], [], [], {}, "", trends_error

    key_trends, key_trends_error = _pdf_generation_text_list(
        key_trends_value,
        "keyTrends",
        max_items=10,
        max_length=4000,
    )
    if key_trends_error:
        return UUID(int=0), [], [], [], {}, "", key_trends_error

    structured_sections, structured_error = _pdf_generation_structured_sections(body)
    if structured_error:
        return UUID(int=0), [], [], [], {}, "", structured_error

    has_content = bool(opportunities or trends or key_trends or structured_sections or additional_notes)
    if not has_content:
        return UUID(int=0), [], [], [], {}, "", _error_response(
            400,
            "missing_pdf_content",
            "At least one report section or note is required.",
        )

    return upload_id, opportunities, trends, key_trends, structured_sections, additional_notes, None


def _pdf_generation_structured_sections(body: dict[str, Any]) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    executive_summary, summary_error = _pdf_generation_structured_summary(
        body.get("executiveSummary")
    )
    if summary_error:
        return {}, summary_error

    ranked_findings, findings_error = _pdf_generation_ranked_findings(
        body.get("rankedFindings", [])
    )
    if findings_error:
        return {}, findings_error

    structured_trends, trends_error = _pdf_generation_text_list(
        body.get("structuredTrends", []),
        "structuredTrends",
        max_items=20,
        max_length=4000,
    )
    if trends_error:
        return {}, trends_error

    action_plan_items, action_plan_error = _pdf_generation_text_list(
        body.get("actionPlanItems", []),
        "actionPlanItems",
        max_items=20,
        max_length=4000,
    )
    if action_plan_error:
        return {}, action_plan_error

    data_notes, data_notes_error = _pdf_generation_text_list(
        body.get("dataNotes", []),
        "dataNotes",
        max_items=20,
        max_length=4000,
    )
    if data_notes_error:
        return {}, data_notes_error

    structured_sections = {
        "executive_summary": executive_summary,
        "ranked_findings": ranked_findings,
        "structured_trends": structured_trends,
        "action_plan_items": action_plan_items,
        "data_notes": data_notes,
    }
    if not any(
        (
            any(executive_summary.values()),
            ranked_findings,
            structured_trends,
            action_plan_items,
            data_notes,
        )
    ):
        return {}, None
    return structured_sections, None


def _pdf_generation_structured_summary(value: object) -> tuple[dict[str, str], Optional[JSONResponse]]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        return {}, _error_response(400, "invalid_executive_summary", "executiveSummary must be an object.")

    summary: dict[str, str] = {}
    for key in ("summary", "primaryConcern", "recommendedFocus"):
        text, text_error = _pdf_generation_text(
            value.get(key, ""),
            f"executiveSummary.{key}",
            4000,
            allow_empty=True,
        )
        if text_error:
            return {}, text_error
        summary[key] = text
    return summary, None


def _pdf_generation_ranked_findings(value: object) -> tuple[list[dict[str, Any]], Optional[JSONResponse]]:
    if value is None:
        value = []
    if not isinstance(value, list):
        return [], _error_response(400, "invalid_ranked_findings", "rankedFindings must be a list.")
    if len(value) > 20:
        return [], _error_response(400, "too_many_ranked_findings", "rankedFindings must include 20 items or fewer.")

    findings: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return [], _error_response(400, "invalid_ranked_finding", "Each ranked finding must be an object.")

        finding, finding_error = _pdf_generation_ranked_finding(item, index)
        if finding_error:
            return [], finding_error
        if finding:
            findings.append(finding)
    return findings, None


def _pdf_generation_ranked_finding(
    item: dict[str, Any],
    index: int,
) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    rank_value = item.get("rank")
    rank = index + 1
    if rank_value is not None:
        if isinstance(rank_value, bool):
            return {}, _error_response(400, "invalid_ranked_finding_rank", f"rankedFindings[{index}].rank must be a number.")
        try:
            rank = int(rank_value)
        except (TypeError, ValueError):
            return {}, _error_response(400, "invalid_ranked_finding_rank", f"rankedFindings[{index}].rank must be a number.")
        if rank < 1:
            rank = index + 1

    text_fields = (
        "title",
        "category",
        "severity",
        "confidence",
        "estimatedImpactCategory",
        "implementationDifficulty",
        "financialValue",
        "clientFacingSummary",
        "operationalImplication",
        "recommendedAction",
    )
    finding: dict[str, Any] = {"rank": rank}
    for field in text_fields:
        text, text_error = _pdf_generation_text(
            item.get(field, ""),
            f"rankedFindings[{index}].{field}",
            4000,
            allow_empty=True,
        )
        if text_error:
            return {}, text_error
        finding[field] = text

    evidence, evidence_error = _pdf_generation_ranked_finding_evidence(
        item.get("evidence", []),
        index,
    )
    if evidence_error:
        return {}, evidence_error
    finding["evidence"] = evidence

    if not any(
        (
            finding["title"],
            finding["financialValue"],
            finding["operationalImplication"],
            finding["recommendedAction"],
            evidence,
        )
    ):
        return {}, None
    return finding, None


def _pdf_generation_ranked_finding_evidence(
    value: object,
    finding_index: int,
) -> tuple[list[dict[str, str]], Optional[JSONResponse]]:
    if value is None:
        value = []
    if not isinstance(value, list):
        return [], _error_response(400, "invalid_ranked_finding_evidence", "ranked finding evidence must be a list.")
    if len(value) > 5:
        return [], _error_response(400, "too_many_ranked_finding_evidence", "ranked finding evidence must include 5 items or fewer.")

    evidence_items: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            return [], _error_response(400, "invalid_ranked_finding_evidence", "Each evidence item must be an object.")
        evidence: dict[str, str] = {}
        for field in ("label", "value", "sourceHint"):
            text, text_error = _pdf_generation_text(
                item.get(field, ""),
                f"rankedFindings[{finding_index}].evidence[{index}].{field}",
                1000,
                allow_empty=True,
            )
            if text_error:
                return [], text_error
            evidence[field] = text
        if any(evidence.values()):
            evidence_items.append(evidence)
    return evidence_items, None


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


def _upload_is_voided(upload: Upload) -> bool:
    return bool(getattr(upload, "voided_at", None))


def _filter_uploads_by_void_status(query: Any, upload_status: str) -> Any:
    normalized_status = (upload_status or "active").strip().lower()
    if normalized_status == "voided":
        return query.filter(Upload.voided_at.isnot(None))
    if normalized_status == "all":
        return query
    return query.filter(Upload.voided_at.is_(None))


def _recurring_checkout_filter() -> Any:
    return or_(
        func.lower(StripeCheckoutSession.billing_mode) == "recurring",
        func.lower(StripeCheckoutSession.mode) == "subscription",
    )


def _checkout_paid_filter() -> Any:
    return or_(
        func.lower(StripeCheckoutSession.payment_status) == "paid",
        func.lower(StripeCheckoutSession.status).in_(["complete", "completed"]),
    )


def _checkout_expired_filter() -> Any:
    return or_(
        func.lower(StripeCheckoutSession.status) == "expired",
        StripeCheckoutSession.expired_at.isnot(None),
    )


def _checkout_open_filter() -> Any:
    return and_(
        or_(
            StripeCheckoutSession.payment_status.is_(None),
            func.lower(StripeCheckoutSession.payment_status) != "paid",
        ),
        or_(
            StripeCheckoutSession.status.is_(None),
            ~func.lower(StripeCheckoutSession.status).in_(["complete", "completed", "expired"]),
        ),
        StripeCheckoutSession.expired_at.is_(None),
    )


def _paid_or_complete_checkout_filter() -> Any:
    return _checkout_paid_filter()


def _upload_has_paid_checkout_session(db: Any, upload_id: UUID) -> bool:
    legacy_session = (
        db.query(StripeCheckoutSession.id)
        .filter(StripeCheckoutSession.upload_id == upload_id)
        .filter(_paid_or_complete_checkout_filter())
        .first()
    )
    if legacy_session:
        return True

    linked_session = (
        db.query(StripeCheckoutSession.id)
        .join(
            StripeCheckoutSessionUpload,
            StripeCheckoutSessionUpload.checkout_session_id == StripeCheckoutSession.id,
        )
        .filter(StripeCheckoutSessionUpload.upload_id == upload_id)
        .filter(_paid_or_complete_checkout_filter())
        .first()
    )
    return bool(linked_session)


def _checkout_related_uploads_by_session(
    db: Any,
    sessions: list[StripeCheckoutSession],
    upload_status: str = "active",
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

    link_query = (
        db.query(StripeCheckoutSessionUpload, Upload)
        .join(Upload, Upload.id == StripeCheckoutSessionUpload.upload_id)
        .filter(StripeCheckoutSessionUpload.checkout_session_id.in_(session_ids))
    )
    link_query = _filter_uploads_by_void_status(link_query, upload_status)
    link_rows = link_query.order_by(StripeCheckoutSessionUpload.created_at.asc()).all()
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

    legacy_upload_query = db.query(Upload).filter(Upload.id.in_(legacy_upload_ids))
    legacy_upload_query = _filter_uploads_by_void_status(legacy_upload_query, upload_status)
    legacy_upload_rows = legacy_upload_query.all()
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


def _recover_missing_recurring_subscription_sessions(
    db: Any,
    sessions: list[StripeCheckoutSession],
) -> None:
    stripe_secret_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not stripe_secret_key:
        return

    for session in sessions:
        if not _checkout_session_needs_subscription_recovery(session):
            continue
        try:
            recovered = _recover_recurring_subscription_session(
                db,
                session,
                stripe_secret_key,
                datetime.now(timezone.utc),
            )
            if recovered:
                db.commit()
        except stripe.error.StripeError as error:
            db.rollback()
            logger.warning(
                "[admin_api] recurring subscription recovery Stripe lookup failed checkout_session_id=%s error_type=%s",
                _clean_text(getattr(session, "stripe_checkout_session_id", None)),
                error.__class__.__name__,
            )
        except Exception:
            db.rollback()
            logger.exception(
                "[admin_api] recurring subscription recovery failed checkout_session_id=%s",
                _clean_text(getattr(session, "stripe_checkout_session_id", None)),
            )


def _recover_recurring_subscription_session(
    db: Any,
    local_session: StripeCheckoutSession,
    stripe_secret_key: str,
    now: datetime,
) -> bool:
    checkout_session_id = _clean_text(getattr(local_session, "stripe_checkout_session_id", None))
    if not checkout_session_id:
        return False

    stripe_session = stripe.checkout.Session.retrieve(
        checkout_session_id,
        expand=["subscription", "subscription.latest_invoice"],
        api_key=stripe_secret_key,
    )
    session_data = _stripe_object_to_dict(stripe_session)
    if not session_data:
        return False

    subscription_value = session_data.get("subscription")
    subscription_id = _stripe_object_id(subscription_value, "sub_")
    _apply_checkout_session_data(local_session, session_data, now)
    if not subscription_id:
        logger.info(
            "[admin_api] recurring subscription recovery found no subscription checkout_session_id=%s status=%s payment_status=%s",
            checkout_session_id,
            _clean_text(session_data.get("status")),
            _clean_text(session_data.get("payment_status")),
        )
        return True

    local_session.stripe_subscription_id = subscription_id
    subscription_data = _stripe_expanded_object_to_dict(subscription_value)
    if not subscription_data:
        subscription_data = {"id": subscription_id, "metadata": _stripe_metadata_from_checkout_session(local_session)}
    if not subscription_data.get("current_period_end") or not subscription_data.get("status"):
        retrieved_subscription = stripe.Subscription.retrieve(
            subscription_id,
            expand=["latest_invoice"],
            api_key=stripe_secret_key,
        )
        subscription_data = _stripe_object_to_dict(retrieved_subscription) or subscription_data

    cancel_at = _stripe_timestamp_to_datetime(subscription_data.get("cancel_at"))
    cancel_schedule_status = "scheduled" if cancel_at else None
    local_subscription = _upsert_stripe_subscription(
        db,
        subscription_data,
        now,
        local_session=local_session,
        session_data=session_data,
        cancel_schedule_status=cancel_schedule_status,
    )
    if not local_subscription:
        _update_checkout_offer_metadata(
            local_session,
            {
                "cancelScheduleStatus": "needs_review",
                "cancelScheduleReason": "subscription_recovery_validation_failed",
            },
        )
        return True

    if cancel_at:
        _update_checkout_offer_metadata(
            local_session,
            {
                "cancelScheduleStatus": "scheduled",
                "cancelAt": _iso_datetime(cancel_at),
            },
        )
    elif _checkout_session_cancel_schedule_status(local_session) == "pending_checkout_completion":
        intended_cancel_at = _retainer_cancel_at(subscription_data, local_session, session_data, now)
        recovery_metadata: dict[str, Any] = {
            "cancelScheduleStatus": "needs_review",
            "cancelScheduleReason": "subscription_recovery_missing_cancel_at",
        }
        if intended_cancel_at:
            recovery_metadata["intendedCancelAt"] = _iso_datetime(intended_cancel_at)
        _update_checkout_offer_metadata(local_session, recovery_metadata)
        _merge_subscription_metadata(local_subscription, recovery_metadata)

    _apply_subscription_to_checkout_sessions(db, local_subscription, now)
    logger.info(
        "[admin_api] recurring subscription recovery synced checkout_session_id=%s subscription_id=%s status=%s",
        checkout_session_id,
        subscription_id,
        _clean_text(getattr(local_subscription, "status", None)),
    )
    return True


def _checkout_subscriptions_by_session(
    db: Any,
    sessions: list[StripeCheckoutSession],
) -> dict[str, StripeSubscription]:
    session_ids = [
        getattr(session, "id")
        for session in sessions
        if getattr(session, "id", None)
    ]
    stripe_checkout_session_ids = [
        _clean_text(getattr(session, "stripe_checkout_session_id", None))
        for session in sessions
        if _clean_text(getattr(session, "stripe_checkout_session_id", None))
    ]
    stripe_subscription_ids = [
        _clean_text(getattr(session, "stripe_subscription_id", None))
        for session in sessions
        if _clean_text(getattr(session, "stripe_subscription_id", None))
    ]
    if not session_ids and not stripe_checkout_session_ids and not stripe_subscription_ids:
        return {}

    filters = []
    if session_ids:
        filters.append(StripeSubscription.source_checkout_session_id.in_(session_ids))
    if stripe_checkout_session_ids:
        filters.append(StripeSubscription.stripe_checkout_session_id.in_(stripe_checkout_session_ids))
    if stripe_subscription_ids:
        filters.append(StripeSubscription.stripe_subscription_id.in_(stripe_subscription_ids))
    if not filters:
        return {}

    subscriptions = (
        db.query(StripeSubscription)
        .filter(or_(*filters))
        .order_by(StripeSubscription.updated_at.desc())
        .all()
    )
    by_key: dict[tuple[str, str], StripeSubscription] = {}
    for subscription in subscriptions:
        local_session_id = _id_text(getattr(subscription, "source_checkout_session_id", None))
        stripe_checkout_session_id = _clean_text(getattr(subscription, "stripe_checkout_session_id", None))
        stripe_subscription_id = _clean_text(getattr(subscription, "stripe_subscription_id", None))
        if local_session_id:
            by_key.setdefault(("local", local_session_id), subscription)
        if stripe_checkout_session_id:
            by_key.setdefault(("checkout", stripe_checkout_session_id), subscription)
        if stripe_subscription_id:
            by_key.setdefault(("subscription", stripe_subscription_id), subscription)

    subscriptions_by_session: dict[str, StripeSubscription] = {}
    for session in sessions:
        session_id = _id_text(getattr(session, "id", None))
        if not session_id:
            continue
        stripe_checkout_session_id = _clean_text(getattr(session, "stripe_checkout_session_id", None))
        stripe_subscription_id = _clean_text(getattr(session, "stripe_subscription_id", None))
        subscription = (
            by_key.get(("local", session_id))
            or (by_key.get(("checkout", stripe_checkout_session_id)) if stripe_checkout_session_id else None)
            or (by_key.get(("subscription", stripe_subscription_id)) if stripe_subscription_id else None)
        )
        if subscription:
            subscriptions_by_session[session_id] = subscription

    return subscriptions_by_session


def _subscription_metadata_text(
    subscription: Optional[StripeSubscription],
    session: StripeCheckoutSession,
    key: str,
) -> str:
    for metadata in (
        getattr(subscription, "subscription_metadata", None) if subscription else None,
        getattr(session, "offer_metadata", None),
    ):
        if isinstance(metadata, dict):
            value = _clean_text(metadata.get(key))
            if value:
                return value
    return ""


def _checkout_session_payload(
    session: StripeCheckoutSession,
    related_uploads: Optional[list[Upload]] = None,
    subscription: Optional[StripeSubscription] = None,
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

    stripe_subscription_id = (
        _clean_text(getattr(subscription, "stripe_subscription_id", None))
        if subscription
        else ""
    ) or _clean_text(getattr(session, "stripe_subscription_id", None))
    subscription_status = (
        _clean_text(getattr(subscription, "status", None))
        if subscription
        else ""
    ) or _clean_text(getattr(session, "subscription_status", None))
    subscription_current_period_start = (
        getattr(subscription, "current_period_start", None)
        if subscription
        else None
    )
    subscription_current_period_end = (
        getattr(subscription, "current_period_end", None)
        if subscription
        else None
    ) or getattr(session, "current_period_end", None)
    subscription_cancel_at = (
        getattr(subscription, "cancel_at", None)
        if subscription
        else None
    ) or getattr(session, "cancel_at", None)
    latest_payment_status = (
        _clean_text(getattr(subscription, "latest_payment_status", None))
        if subscription
        else ""
    )
    cancel_schedule_status = _subscription_metadata_text(
        subscription,
        session,
        "cancelScheduleStatus",
    )

    return {
        "id": _id_text(getattr(session, "id", None)),
        "stripeCheckoutSessionId": _clean_text(
            getattr(session, "stripe_checkout_session_id", None)
        ),
        "stripeCustomerId": _clean_text(getattr(session, "stripe_customer_id", None)),
        "clientEmail": _clean_text(getattr(session, "client_email", None)),
        "purpose": _clean_text(getattr(session, "purpose", None)),
        "description": _clean_text(getattr(session, "description", None)),
        "offerType": _clean_text(getattr(session, "offer_type", None)),
        "offerName": _clean_text(getattr(session, "offer_name", None)),
        "billingMode": _clean_text(getattr(session, "billing_mode", None)),
        "interval": _clean_text(getattr(session, "interval", None))
        or (_clean_text(getattr(subscription, "interval", None)) if subscription else ""),
        "monthlyAmount": _optional_int(getattr(session, "monthly_amount", None))
        or (_optional_int(getattr(subscription, "monthly_amount", None)) if subscription else None),
        "contractMonths": _optional_int(getattr(session, "contract_months", None))
        or (_optional_int(getattr(subscription, "contract_months", None)) if subscription else None),
        "stripeSubscriptionId": stripe_subscription_id,
        "subscriptionStatus": subscription_status,
        "subscriptionCurrentPeriodStart": _iso_datetime(subscription_current_period_start),
        "subscriptionCurrentPeriodEnd": _iso_datetime(subscription_current_period_end),
        "subscriptionCancelAt": _iso_datetime(subscription_cancel_at),
        "subscriptionCancelAtPeriodEnd": (
            bool(getattr(subscription, "cancel_at_period_end", False))
            if subscription and getattr(subscription, "cancel_at_period_end", None) is not None
            else None
        ),
        "subscriptionCanceledAt": _iso_datetime(
            getattr(subscription, "canceled_at", None) if subscription else None
        ),
        "latestPaymentStatus": latest_payment_status,
        "cancelScheduleStatus": cancel_schedule_status,
        "currentPeriodEnd": _iso_datetime(subscription_current_period_end),
        "cancelAt": _iso_datetime(subscription_cancel_at),
        "internalNote": _clean_text(getattr(session, "internal_note", None)),
        "mode": _clean_text(getattr(session, "mode", None)),
        "status": _clean_text(getattr(session, "status", None)),
        "paymentStatus": _clean_text(getattr(session, "payment_status", None)),
        "amountTotal": _optional_int(getattr(session, "amount_total", None)),
        "currency": _clean_text(getattr(session, "currency", None)),
        "checkoutUrl": _clean_text(getattr(session, "checkout_url", None)),
        "expiresAt": _iso_datetime(getattr(session, "expires_at", None)),
        "expiredAt": _iso_datetime(getattr(session, "expired_at", None)),
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
        "voided": _upload_is_voided(upload),
        "voidedAt": _iso_datetime(getattr(upload, "voided_at", None)),
        "voidReason": _clean_text(getattr(upload, "void_reason", None)),
        "voidedByAdminEmail": _clean_text(getattr(upload, "voided_by_admin_email", None)),
    }


def _client_recent_submission_payload(
    submission: ClientSubmission,
    upload: Optional[Upload] = None,
) -> dict[str, Any]:
    return {
        "id": _id_text(getattr(submission, "id", None)),
        "status": _clean_text(getattr(submission, "status", None)),
        "source": _clean_text(getattr(submission, "source", None)),
        "submittedAt": _iso_datetime(getattr(submission, "submitted_at", None)),
        "completedAt": _iso_datetime(getattr(submission, "completed_at", None)),
        "canceledAt": _iso_datetime(getattr(submission, "canceled_at", None)),
        "erroredAt": _iso_datetime(getattr(submission, "errored_at", None)),
        "errorMessage": _clean_text(getattr(submission, "error_message", None)),
        "ghlCid": _clean_text(getattr(submission, "ghl_cid", None)),
        "upload": _client_recent_submission_upload_payload(upload) if upload else None,
    }


def _client_recent_submission_upload_payload(upload: Upload) -> dict[str, Any]:
    return {
        "id": _id_text(getattr(upload, "id", None)),
        "fileName": _clean_text(getattr(upload, "file_name", None)),
        "toolName": _clean_text(getattr(upload, "tool_name", None)),
        "paid": bool(getattr(upload, "paid", False)),
        "voided": _upload_is_voided(upload),
        "uploadTime": _clean_text(getattr(upload, "upload_time", None)),
    }


def _client_consultant_reviews(db: Any, client_email: str) -> list[dict[str, Any]]:
    uploads = (
        db.query(Upload)
        .filter(func.lower(Upload.user_email) == client_email)
        .filter(Upload.analysis_data.isnot(None))
        .filter(Upload.voided_at.is_(None))
        .order_by(Upload.pdf_generated_at.desc(), Upload.upload_time.desc(), Upload.id.desc())
        .limit(50)
        .all()
    )

    reviews: list[dict[str, Any]] = []
    for upload in uploads:
        payload = _client_consultant_review_payload(upload)
        if payload:
            reviews.append(payload)
        if len(reviews) >= 10:
            break
    return reviews


def _client_consultant_review_payload(upload: Upload) -> Optional[dict[str, Any]]:
    analysis_payload = _pdf_generator_analysis_payload(getattr(upload, "analysis_data", None))
    if not isinstance(analysis_payload, dict):
        return None

    structured_analysis = analysis_payload.get("structured_analysis")
    if not isinstance(structured_analysis, dict):
        return None

    provider_structured_statuses = analysis_payload.get("structured_provider_statuses")
    raw_analyses = analysis_payload.get("raw_analyses")

    return {
        "id": _id_text(getattr(upload, "id", None)),
        "uploadId": _id_text(getattr(upload, "id", None)),
        "fileName": _clean_text(getattr(upload, "file_name", None)),
        "toolName": _clean_text(getattr(upload, "tool_name", None)),
        "uploadTime": _clean_text(getattr(upload, "upload_time", None)),
        "paid": bool(getattr(upload, "paid", False)),
        "voided": _upload_is_voided(upload),
        "pdfGeneratedAt": _iso_datetime(getattr(upload, "pdf_generated_at", None)),
        "structuredAnalysis": structured_analysis,
        "providerStructuredStatuses": (
            provider_structured_statuses if isinstance(provider_structured_statuses, dict) else {}
        ),
        "rawAnalyses": raw_analyses if isinstance(raw_analyses, dict) else {},
        "generatedAt": _clean_text(analysis_payload.get("generatedAt")),
        "sourceFormat": _clean_text(analysis_payload.get("sourceFormat")),
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


def _secure_upload_download_filename(value: object) -> str:
    text = _clean_text(value) or "secure-upload-file"
    cleaned = text.replace("\x00", "").replace("\r", "").replace("\n", "")
    file_name = os.path.basename(cleaned).strip()
    return file_name or "secure-upload-file"


def _secure_upload_download_content_type(value: object) -> str:
    content_type = _clean_text(value) or "application/octet-stream"
    if "\r" in content_type or "\n" in content_type or len(content_type) > 200:
        return "application/octet-stream"
    return content_type


def _positive_int(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


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


def _request_user_agent(request: Request) -> Optional[str]:
    user_agent = _clean_text(request.headers.get("user-agent"))
    if not user_agent:
        return None
    return user_agent[:500]


def _audit_device_summary(user_agent: object) -> str:
    user_agent_text = _clean_text(user_agent)
    if not user_agent_text:
        return "Unknown"

    lowered = user_agent_text.lower()
    if "edg/" in lowered or "edge/" in lowered:
        browser = "Edge"
    elif "firefox/" in lowered:
        browser = "Firefox"
    elif "chrome/" in lowered or "crios/" in lowered:
        browser = "Chrome"
    elif "safari/" in lowered:
        browser = "Safari"
    else:
        browser = "Unknown browser"

    if "iphone" in lowered or "ipad" in lowered:
        platform = "iOS"
    elif "android" in lowered:
        platform = "Android"
    elif "mac os x" in lowered or "macintosh" in lowered:
        platform = "macOS"
    elif "windows" in lowered:
        platform = "Windows"
    elif "linux" in lowered:
        platform = "Linux"
    else:
        platform = "Unknown device"

    if browser == "Unknown browser" and platform == "Unknown device":
        return "Unknown"
    if browser == "Unknown browser":
        return platform
    if platform == "Unknown device":
        return browser
    return f"{browser} on {platform}"


def _audit_metadata_key_allowed(key: object) -> bool:
    normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
    if normalized in ADMIN_AUDIT_METADATA_DENY_KEYS:
        return False
    return not any(token in normalized for token in ("password", "secret", "apikey", "signedurl", "url"))


def _sanitize_audit_metadata_value(value: object, depth: int = 0) -> object:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, (datetime, UUID)):
        return str(value)
    if isinstance(value, str):
        cleaned = value.replace("\x00", "").strip()
        return cleaned[:ADMIN_AUDIT_METADATA_MAX_STRING] if cleaned else ""
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 50:
                sanitized["truncated"] = True
                break
            key_text = str(key)
            if not _audit_metadata_key_allowed(key_text):
                continue
            sanitized[key_text[:100]] = _sanitize_audit_metadata_value(item, depth + 1)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_audit_metadata_value(item, depth + 1) for item in list(value)[:50]]
    return str(value)[:ADMIN_AUDIT_METADATA_MAX_STRING]


def _sanitize_audit_metadata(metadata: object) -> dict[str, object]:
    if not isinstance(metadata, dict):
        metadata = {}
    sanitized = _sanitize_audit_metadata_value(metadata)
    if not isinstance(sanitized, dict):
        sanitized = {}

    try:
        serialized = json.dumps(sanitized, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"metadataSanitized": True}
    if len(serialized.encode("utf-8")) > ADMIN_AUDIT_METADATA_MAX_BYTES:
        return {"metadataTruncated": True}
    return sanitized


def _record_admin_audit_event(
    db: Any,
    request: Optional[Request],
    event_type: str,
    target_type: Optional[str] = None,
    target_id: Optional[object] = None,
    client_email: Optional[str] = None,
    metadata: Optional[dict[str, object]] = None,
    admin_auth_user: Optional[dict[str, Any]] = None,
    admin_access: Optional[AdminUser] = None,
    source: str = "admin_api",
    occurred_at: Optional[datetime] = None,
) -> None:
    del db
    user_agent = _request_user_agent(request) if request else None
    ip_address = _request_client_ip(request) if request else None
    actor_admin_user_id = (
        _clean_text((admin_auth_user or {}).get("id"))
        or _id_text(getattr(admin_access, "user_id", None))
    )
    actor_admin_email = (
        _clean_text(getattr(admin_access, "email", None))
        or _clean_text((admin_auth_user or {}).get("email"))
    )

    audit_db = SessionLocal()
    try:
        audit_db.add(
            AdminAuditEvent(
                occurred_at=occurred_at or datetime.now(timezone.utc),
                source=_clean_text(source) or "admin_api",
                event_type=_clean_text(event_type) or "unknown",
                actor_admin_user_id=actor_admin_user_id,
                actor_admin_email=actor_admin_email,
                actor_display_name=_clean_text(getattr(admin_access, "display_name", None)),
                actor_role=_clean_text(getattr(admin_access, "role", None)),
                client_email=(_clean_text(client_email) or "").lower() or None,
                target_type=_clean_text(target_type),
                target_id=_id_text(target_id),
                ip_address=ip_address,
                user_agent=user_agent,
                device_summary=_audit_device_summary(user_agent),
                location="Unknown" if ip_address else None,
                metadata_json=_sanitize_audit_metadata(metadata or {}),
            )
        )
        audit_db.commit()
    except Exception:
        audit_db.rollback()
        logger.warning(
            "[admin_audit] event write failed event_type=%s target_type=%s target_id=%s",
            event_type,
            target_type,
            _id_text(target_id),
        )
    finally:
        audit_db.close()


def _validate_admin_analysis_phi_acknowledgment(
    body: Optional[dict[str, Any]],
) -> tuple[Optional[str], Optional[JSONResponse]]:
    if not isinstance(body, dict):
        return None, _error_response(
            400,
            "missing_phi_acknowledgment",
            "PHI acknowledgment is required before processing.",
        )

    acknowledgment = body.get("phiAcknowledgment")
    if not isinstance(acknowledgment, dict):
        return None, _error_response(
            400,
            "missing_phi_acknowledgment",
            "PHI acknowledgment is required before processing.",
        )

    if acknowledgment.get("confirmedNoPhi") is not True:
        return None, _error_response(
            400,
            "phi_acknowledgment_required",
            "Confirm the file is approved/sanitized and does not contain unsanitized PHI before processing.",
        )

    initials = _clean_text(acknowledgment.get("initials"))
    if not initials or len(initials) > 12:
        return None, _error_response(
            400,
            "invalid_phi_acknowledgment_initials",
            "Acknowledgment initials are required and must be 12 characters or fewer.",
        )

    return initials, None


def _record_admin_analysis_phi_acknowledgment(
    db: Any,
    *,
    request: Request,
    body: Optional[dict[str, Any]],
    job: AdminAnalysisJob,
    job_file: AdminAnalysisJobFile,
    admin_auth_user: dict[str, Any],
    admin_access: Optional[AdminUser],
) -> Optional[JSONResponse]:
    initials, validation_error = _validate_admin_analysis_phi_acknowledgment(body)
    if validation_error:
        return validation_error

    admin_user_id = _clean_text((admin_auth_user or {}).get("id"))
    admin_email = (
        _clean_text(getattr(admin_access, "email", None))
        or _clean_text((admin_auth_user or {}).get("email"))
    )
    db.add(
        AdminAnalysisPhiAcknowledgment(
            job_id=getattr(job, "id", None),
            job_file_id=getattr(job_file, "id", None),
            tool_name=_clean_text(getattr(job_file, "tool_name", None)),
            admin_user_id=admin_user_id,
            admin_email=admin_email,
            initials=initials or "",
            confirmed_no_phi=True,
            acknowledgment_text=ADMIN_ANALYSIS_PHI_ACK_TEXT,
            acknowledgment_version=ADMIN_ANALYSIS_PHI_ACK_VERSION,
            ip_address=_request_client_ip(request),
            user_agent=_request_user_agent(request),
        )
    )
    db.flush()
    _record_admin_audit_event(
        db,
        request,
        "analysis.phi_acknowledged",
        target_type="admin_analysis_job",
        target_id=getattr(job, "id", None),
        client_email=_clean_text(getattr(job, "client_email", None)),
        metadata={
            "jobFileId": _id_text(getattr(job_file, "id", None)),
            "toolName": _clean_text(getattr(job_file, "tool_name", None)),
            "initials": initials,
            "acknowledgmentVersion": ADMIN_ANALYSIS_PHI_ACK_VERSION,
        },
        admin_auth_user=admin_auth_user,
        admin_access=admin_access,
    )
    logger.info(
        "[admin_analysis] PHI acknowledgment accepted job_id=%s job_file_id=%s admin_user_id=%s",
        getattr(job, "id", None),
        getattr(job_file, "id", None),
        admin_user_id,
    )
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
            "customer",
            "subscription",
            "expires_at",
            "expired_at",
            "created",
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


def _required_limited_text(
    value: object,
    field_name: str,
    max_length: int,
) -> tuple[str, Optional[JSONResponse]]:
    text, validation_error = _required_text(value, field_name)
    if validation_error:
        return "", validation_error
    if len(text) > max_length:
        return "", _error_response(
            400,
            f"invalid_{field_name}",
            f"{field_name} must be {max_length} characters or fewer.",
        )
    return text, None


def _optional_limited_text(
    value: object,
    field_name: str,
    max_length: int,
) -> tuple[Optional[str], Optional[JSONResponse]]:
    text = _clean_text(value)
    if not text:
        return None, None
    if len(text) > max_length:
        return None, _error_response(
            400,
            f"invalid_{field_name}",
            f"{field_name} must be {max_length} characters or fewer.",
        )
    return text, None


def _required_amount(value: object) -> tuple[int, Optional[JSONResponse]]:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0, _error_response(400, "invalid_amount", "amount must be an integer number of cents.")
    if value <= 0:
        return 0, _error_response(400, "invalid_amount", "amount must be greater than zero.")
    if value > 10000000:
        return 0, _error_response(400, "invalid_amount", "amount is too large.")
    return value, None


def _required_contract_months(value: object) -> tuple[int, Optional[JSONResponse]]:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0, _error_response(400, "invalid_contract_months", "contractMonths must be an integer.")
    if value < ADMIN_RETAINER_CONTRACT_MONTHS_MIN or value > ADMIN_RETAINER_CONTRACT_MONTHS_MAX:
        return 0, _error_response(
            400,
            "invalid_contract_months",
            f"contractMonths must be between {ADMIN_RETAINER_CONTRACT_MONTHS_MIN} and {ADMIN_RETAINER_CONTRACT_MONTHS_MAX}.",
        )
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

    voided_upload_ids = [
        str(selected_upload_id)
        for selected_upload_id in upload_ids
        if _upload_is_voided(uploads_by_id[str(selected_upload_id)])
    ]
    if voided_upload_ids:
        return [], _error_response(409, "voided_upload_cannot_be_used", "Voided uploads cannot be used.")

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


def _stripe_timestamp_to_datetime(value: object) -> Optional[datetime]:
    if value is None or isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
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
        "canWriteClients": _admin_has_permission(admin_user, PERMISSION_CLIENTS_WRITE),
        "canWriteUploads": _admin_has_permission(admin_user, PERMISSION_UPLOADS_WRITE),
        "canReadBilling": _admin_has_permission(admin_user, PERMISSION_BILLING_READ),
        "canWriteBilling": _admin_has_permission(admin_user, PERMISSION_BILLING_WRITE),
        "canReadAnalysis": _admin_has_permission(admin_user, PERMISSION_ANALYSIS_READ),
        "canWriteAnalysis": _admin_has_permission(admin_user, PERMISSION_ANALYSIS_WRITE),
        "canReadPdf": _admin_has_permission(admin_user, PERMISSION_PDF_READ),
        "canGeneratePdf": _admin_has_permission(admin_user, PERMISSION_PDF_GENERATE),
        "canReadSecureUploads": _admin_has_permission(admin_user, PERMISSION_SECURE_UPLOADS_READ),
        "canWriteSecureUploads": _admin_has_permission(admin_user, PERMISSION_SECURE_UPLOADS_WRITE),
        "canReadAgreements": _admin_has_permission(admin_user, PERMISSION_AGREEMENTS_READ),
        "canWriteAgreements": _admin_has_permission(admin_user, PERMISSION_AGREEMENTS_WRITE),
        "canReadAdminManagement": _admin_has_permission(admin_user, PERMISSION_ADMIN_MANAGEMENT_READ),
        "canManageAdminAccess": _admin_has_permission(admin_user, PERMISSION_ADMIN_MANAGEMENT_WRITE),
        "canReadAudit": _admin_has_permission(admin_user, PERMISSION_AUDIT_READ),
        "canReadSiteAnalytics": _admin_has_permission(admin_user, PERMISSION_SITE_ANALYTICS_READ),
        "canManageSiteAnalytics": _admin_has_permission(admin_user, PERMISSION_SITE_ANALYTICS_WRITE),
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
        "expiredCheckoutSessionCount": 0,
        "manualOverrideCount": 0,
        "latestPaymentStatus": None,
    }


def _first_present_text(*values: object) -> Optional[str]:
    for value in values:
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return None


def _client_billing_profile_payload(
    client_email: str,
    user: Optional[User],
    latest_submission: Optional[ClientSubmission],
) -> dict[str, Optional[str]]:
    first_name = _first_present_text(
        getattr(user, "first_name", None),
        getattr(latest_submission, "first_name", None),
    )
    last_name = _first_present_text(
        getattr(user, "last_name", None),
        getattr(latest_submission, "last_name", None),
    )
    full_name = " ".join(part for part in (first_name, last_name) if part).strip() or None

    return {
        "name": full_name,
        "firstName": first_name,
        "lastName": last_name,
        "email": _first_present_text(
            getattr(user, "email", None),
            getattr(latest_submission, "user_email", None),
            client_email,
        ),
        "officeName": _first_present_text(
            getattr(user, "office_name", None),
            getattr(latest_submission, "office_name", None),
        ),
        "orgType": _first_present_text(
            getattr(user, "org_type", None),
            getattr(latest_submission, "org_type", None),
        ),
        "phone": _first_present_text(
            getattr(user, "phone", None),
            getattr(latest_submission, "phone", None),
        ),
        "latestGhlCid": _clean_text(getattr(latest_submission, "ghl_cid", None)),
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


def _user_full_name(user: Optional[User]) -> Optional[str]:
    if not user:
        return None
    first_name = _clean_text(getattr(user, "first_name", None)) or ""
    last_name = _clean_text(getattr(user, "last_name", None)) or ""
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


def _agreement_error_response(exc: AgreementServiceError) -> JSONResponse:
    return _error_response(exc.status, exc.code, exc.message)


def _agreement_public_error(code: str, message: str, status: int = 400) -> JSONResponse:
    return _error_response(status, code, message)


def _iso_date(value: object) -> Optional[str]:
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            return None
    return None


def _truthy(value: object) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    normalized = str(value).strip().lower()
    return normalized in {"1", "true", "yes", "y", "on"}


def _agreement_client_user_id(db: Any, payload: dict[str, Any]) -> Optional[UUID]:
    explicit_user_id = _uuid_or_none(payload.get("client_user_id"))
    if explicit_user_id:
        return explicit_user_id
    client_email = normalize_agreement_email(payload.get("client_email"))
    if not client_email:
        return None
    user = db.query(User).filter(func.lower(User.email) == client_email).first()
    return getattr(user, "id", None) if user else None


def _agreement_template_values_from_row(agreement: ConsultingAgreement) -> dict[str, Any]:
    snapshot = getattr(agreement, "template_snapshot", None)
    if isinstance(snapshot, dict) and isinstance(snapshot.get("values"), dict):
        values = dict(snapshot["values"])
        if not values.get("signerEmail"):
            values["signerEmail"] = getattr(agreement, "signer_email", None)
        return values
    return payload_template_values(
        {
            "client_email": getattr(agreement, "client_email", None),
            "client_legal_name": getattr(agreement, "client_legal_name", None),
            "office_name": getattr(agreement, "office_name", None),
            "org_type": getattr(agreement, "org_type", None),
            "phone": getattr(agreement, "phone", None),
            "state": getattr(agreement, "state", None),
            "effective_date": getattr(agreement, "effective_date", None),
            "document_type": getattr(agreement, "document_type", None) or AGREEMENT_DOCUMENT_TYPE,
            "signer_name": getattr(agreement, "signer_name", None),
            "signer_email": getattr(agreement, "signer_email", None),
            "signer_title": getattr(agreement, "signer_title", None),
            "ba_signer_name": getattr(agreement, "ba_signer_name", None),
            "ba_signer_title": getattr(agreement, "ba_signer_title", None),
            "ba_signer_email": getattr(agreement, "ba_signer_email", None),
            "ba_signature_mode": getattr(agreement, "ba_signature_mode", None),
        }
    )


def _find_public_agreement_by_token_hash(db: Any, token_hash: str) -> tuple[Optional[ConsultingAgreement], Optional[str]]:
    agreement = db.query(ConsultingAgreement).filter(ConsultingAgreement.signer_token_hash == token_hash).first()
    if agreement:
        return agreement, "client"
    agreement = db.query(ConsultingAgreement).filter(ConsultingAgreement.ba_signer_token_hash == token_hash).first()
    if agreement:
        return agreement, "ba"
    return None, None


def _validate_public_signable_agreement(
    agreement: Optional[ConsultingAgreement],
    signer_role: Optional[str],
) -> Optional[JSONResponse]:
    unavailable = _agreement_public_error(
        "signing_link_unavailable",
        "This signing link is invalid or expired.",
        status=404,
    )
    if not agreement:
        return unavailable
    if signer_role == "client":
        if getattr(agreement, "status", None) != "sent":
            return unavailable
        expires_at = getattr(agreement, "signer_token_expires_at", None)
    elif signer_role == "ba":
        if getattr(agreement, "status", None) != "pending_ba_signature":
            return unavailable
        expires_at = getattr(agreement, "ba_signer_token_expires_at", None)
        client_signature_path = (
            _clean_text(getattr(agreement, "client_signature_image_path", None))
            or _clean_text(getattr(agreement, "signature_image_path", None))
        )
        if not client_signature_path or not getattr(agreement, "client_signed_at", None):
            return unavailable
    else:
        return unavailable
    if isinstance(expires_at, datetime) and expires_at <= agreement_utcnow():
        return unavailable
    if not isinstance(expires_at, datetime):
        return unavailable
    if not _clean_text(getattr(agreement, "draft_pdf_path", None)):
        return unavailable
    return None


def _agreement_payload(agreement: ConsultingAgreement, *, include_snapshot: bool = False) -> dict[str, Any]:
    payload = {
        "id": _id_text(getattr(agreement, "id", None)),
        "clientEmail": _clean_text(getattr(agreement, "client_email", None)),
        "clientUserId": _id_text(getattr(agreement, "client_user_id", None)),
        "clientLegalName": _clean_text(getattr(agreement, "client_legal_name", None)),
        "officeName": _clean_text(getattr(agreement, "office_name", None)),
        "orgType": _clean_text(getattr(agreement, "org_type", None)),
        "phone": _clean_text(getattr(agreement, "phone", None)),
        "state": _clean_text(getattr(agreement, "state", None)),
        "effectiveDate": _iso_date(getattr(agreement, "effective_date", None)),
        "documentType": _clean_text(getattr(agreement, "document_type", None)),
        "status": _clean_text(getattr(agreement, "status", None)),
        "isCurrent": bool(getattr(agreement, "is_current", False)),
        "templateVersion": _clean_text(getattr(agreement, "template_version", None)),
        "hasDraftPdf": bool(_clean_text(getattr(agreement, "draft_pdf_path", None))),
        "hasSignedPdf": bool(_clean_text(getattr(agreement, "signed_pdf_path", None))),
        "signerTokenExpiresAt": _iso_datetime(getattr(agreement, "signer_token_expires_at", None)),
        "baSignerTokenExpiresAt": _iso_datetime(getattr(agreement, "ba_signer_token_expires_at", None)),
        "sentAt": _iso_datetime(getattr(agreement, "sent_at", None)),
        "openedAt": _iso_datetime(getattr(agreement, "opened_at", None)),
        "baOpenedAt": _iso_datetime(getattr(agreement, "ba_opened_at", None)),
        "signerName": _clean_text(getattr(agreement, "signer_name", None)),
        "signerEmail": _clean_text(getattr(agreement, "signer_email", None)),
        "signerTitle": _clean_text(getattr(agreement, "signer_title", None)),
        "signerAuthorityConfirmed": bool(getattr(agreement, "signer_authority_confirmed", False)),
        "signerAccepted": bool(getattr(agreement, "signer_accepted", False)),
        "clientSignedAt": _iso_datetime(getattr(agreement, "client_signed_at", None)),
        "hasClientSignature": bool(
            _clean_text(getattr(agreement, "client_signature_image_path", None))
            or _clean_text(getattr(agreement, "signature_image_path", None))
        ),
        "signedAt": _iso_datetime(getattr(agreement, "signed_at", None)),
        "baSignerName": _clean_text(getattr(agreement, "ba_signer_name", None)),
        "baSignerTitle": _clean_text(getattr(agreement, "ba_signer_title", None)),
        "baSignerEmail": _clean_text(getattr(agreement, "ba_signer_email", None)),
        "baSignatureMode": _clean_text(getattr(agreement, "ba_signature_mode", None)),
        "baSignerAuthorityConfirmed": bool(getattr(agreement, "ba_signer_authority_confirmed", False)),
        "baSignerAccepted": bool(getattr(agreement, "ba_signer_accepted", False)),
        "baSignedAt": _iso_datetime(getattr(agreement, "ba_signed_at", None)),
        "hasBaSignature": bool(_clean_text(getattr(agreement, "ba_signature_image_path", None))),
        "createdByAdminId": _clean_text(getattr(agreement, "created_by_admin_id", None)),
        "createdByAdminEmail": _clean_text(getattr(agreement, "created_by_admin_email", None)),
        "sentByAdminId": _clean_text(getattr(agreement, "sent_by_admin_id", None)),
        "sentByAdminEmail": _clean_text(getattr(agreement, "sent_by_admin_email", None)),
        "createdAt": _iso_datetime(getattr(agreement, "created_at", None)),
        "updatedAt": _iso_datetime(getattr(agreement, "updated_at", None)),
        "voidedAt": _iso_datetime(getattr(agreement, "voided_at", None)),
        "voidedByAdminEmail": _clean_text(getattr(agreement, "voided_by_admin_email", None)),
        "voidReason": _clean_text(getattr(agreement, "void_reason", None)),
        "supersededAt": _iso_datetime(getattr(agreement, "superseded_at", None)),
        "supersededByAgreementId": _id_text(getattr(agreement, "superseded_by_agreement_id", None)),
    }
    if include_snapshot:
        payload["templateSnapshot"] = getattr(agreement, "template_snapshot", None) or {}
    return payload


def _audit_parse_datetime(value: object, field_name: str) -> tuple[Optional[datetime], bool, Optional[JSONResponse]]:
    text = _clean_text(value)
    if not text:
        return None, False, None
    try:
        if len(text) == 10:
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc), True, None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc), False, None
    except ValueError:
        return None, False, _error_response(400, f"invalid_{field_name}", f"{field_name} must be an ISO datetime or YYYY-MM-DD.")


def _audit_date_range(
    start_date: object,
    end_date: object,
) -> tuple[Optional[datetime], Optional[datetime], Optional[JSONResponse]]:
    start_dt, _, start_error = _audit_parse_datetime(start_date, "startDate")
    if start_error:
        return None, None, start_error
    end_dt, end_is_date_only, end_error = _audit_parse_datetime(end_date, "endDate")
    if end_error:
        return None, None, end_error
    if end_dt and end_is_date_only:
        end_dt = end_dt + timedelta(days=1)
    if start_dt and end_dt and end_dt < start_dt:
        return None, None, _error_response(400, "invalid_date_range", "endDate must be on or after startDate.")
    return start_dt, end_dt, None


def _audit_events_filtered_query(
    db: Any,
    *,
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
    event_type: Optional[str],
    client_email: Optional[str],
    actor_email: Optional[str],
    target_type: Optional[str],
) -> Any:
    query = db.query(AdminAuditEvent)
    if start_dt:
        query = query.filter(AdminAuditEvent.occurred_at >= start_dt)
    if end_dt:
        query = query.filter(AdminAuditEvent.occurred_at < end_dt)
    normalized_event_type = _clean_text(event_type)
    if normalized_event_type:
        query = query.filter(AdminAuditEvent.event_type == normalized_event_type)
    normalized_client_email = (_clean_text(client_email) or "").lower()
    if normalized_client_email:
        query = query.filter(func.lower(AdminAuditEvent.client_email).ilike(f"%{normalized_client_email}%"))
    normalized_actor_email = (_clean_text(actor_email) or "").lower()
    if normalized_actor_email:
        query = query.filter(func.lower(AdminAuditEvent.actor_admin_email).ilike(f"%{normalized_actor_email}%"))
    normalized_target_type = _clean_text(target_type)
    if normalized_target_type:
        query = query.filter(AdminAuditEvent.target_type == normalized_target_type)
    return query


def _audit_metadata_json(row: AdminAuditEvent) -> str:
    metadata = getattr(row, "metadata_json", None)
    if not isinstance(metadata, dict):
        metadata = {}
    return json.dumps(metadata, sort_keys=True, default=str)


def _audit_mountain_datetime(value: object) -> Optional[str]:
    if not isinstance(value, datetime):
        return None
    date_value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return date_value.astimezone(ADMIN_AUDIT_MOUNTAIN_TZ).strftime("%Y-%m-%d %I:%M %p %Z")


def _audit_event_payload(row: AdminAuditEvent) -> dict[str, Any]:
    return {
        "id": _id_text(getattr(row, "id", None)),
        "occurredAt": _iso_datetime(getattr(row, "occurred_at", None)),
        "occurredAtMst": _audit_mountain_datetime(getattr(row, "occurred_at", None)),
        "source": _clean_text(getattr(row, "source", None)),
        "eventType": _clean_text(getattr(row, "event_type", None)),
        "actorAdminUserId": _clean_text(getattr(row, "actor_admin_user_id", None)),
        "actorAdminEmail": _clean_text(getattr(row, "actor_admin_email", None)),
        "actorDisplayName": _clean_text(getattr(row, "actor_display_name", None)),
        "actorRole": _clean_text(getattr(row, "actor_role", None)),
        "clientEmail": _clean_text(getattr(row, "client_email", None)),
        "targetType": _clean_text(getattr(row, "target_type", None)),
        "targetId": _clean_text(getattr(row, "target_id", None)),
        "ipAddress": _clean_text(getattr(row, "ip_address", None)),
        "userAgent": _clean_text(getattr(row, "user_agent", None)),
        "deviceSummary": _clean_text(getattr(row, "device_summary", None)),
        "location": _clean_text(getattr(row, "location", None)),
        "metadata": getattr(row, "metadata_json", None) if isinstance(getattr(row, "metadata_json", None), dict) else {},
        "createdAt": _iso_datetime(getattr(row, "created_at", None)),
    }


def _audit_csv_cell(value: object) -> str:
    text = _clean_text(value) or ""
    if text.startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text
