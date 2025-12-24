import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests
from sqlalchemy import func

from database import SessionLocal
from models import UploadPortalFile, UploadPortalRequest, UploadPortalSession, User

logger = logging.getLogger("upload_portal")

SAFE_FILENAME_PATTERN = re.compile(r"[^a-zA-Z0-9._-]+")
OBJECT_NAME_PATTERN = re.compile(
    r"^upload-portal/[0-9a-f-]{36}/\d{4}-\d{2}-\d{2}/[0-9a-f]{12}_[A-Za-z0-9._-]+$"
)
DEFAULT_ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "text/csv",
    "text/plain",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class PortalError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, detail: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _portal_base_url() -> str:
    return _get_env("PORTAL_BASE_URL")


def _token_ttl_minutes() -> int:
    try:
        return int(_get_env("PORTAL_TOKEN_TTL_MINUTES", "60"))
    except ValueError:
        return 60


def _session_ttl_minutes() -> int:
    try:
        return int(_get_env("PORTAL_SESSION_TTL_MINUTES", "30"))
    except ValueError:
        return 30


def _signer_service_url() -> str:
    return _get_env("PORTAL_SIGNER_SERVICE_URL")


def _signer_api_key() -> str:
    return _get_env("PORTAL_SIGNER_API_KEY")


def _gcs_bucket() -> str:
    return _get_env("GCS_BUCKET_NAME")


def _max_file_size_bytes() -> int:
    try:
        mb = int(_get_env("PORTAL_MAX_FILE_SIZE_MB", "50"))
    except ValueError:
        mb = 50
    return mb * 1024 * 1024


def _allowed_content_types() -> set:
    raw = _get_env("PORTAL_ALLOWED_CONTENT_TYPES")
    if raw:
        return {item.strip().lower() for item in raw.split(",") if item.strip()}
    return {item.lower() for item in DEFAULT_ALLOWED_CONTENT_TYPES}


def normalize_email(raw_email: str) -> str:
    if not raw_email:
        return ""
    return raw_email.strip().lower()


def mask_email(raw_email: str) -> str:
    normalized = normalize_email(raw_email)
    if "@" not in normalized:
        return "***"
    local, domain = normalized.split("@", 1)
    local_mask = (local[:1] + "***") if local else "***"
    domain_parts = domain.split(".")
    if len(domain_parts) >= 2:
        domain_mask = f"{domain_parts[0][:1]}***.{domain_parts[-1]}"
    else:
        domain_mask = f"{domain[:1]}***"
    return f"{local_mask}@{domain_mask}"


def _log_event(event: str, **fields: Any) -> None:
    sanitized: Dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        lower_key = key.lower()
        if "email" in lower_key:
            sanitized[key] = mask_email(str(value))
            continue
        if any(token in lower_key for token in ("filename", "object", "path", "token")):
            continue
        sanitized[key] = value
    payload = " ".join(f"{key}={value}" for key, value in sanitized.items())
    logger.info("event=%s %s", event, payload)


def _require_request_config() -> None:
    missing = []
    if not _portal_base_url():
        missing.append("PORTAL_BASE_URL")
    if not _get_env("SENDGRID_API_KEY"):
        missing.append("SENDGRID_API_KEY")
    if not _get_env("FROM_EMAIL"):
        missing.append("FROM_EMAIL")
    if missing:
        raise PortalError("config_missing", "Missing portal configuration", status=500, detail=", ".join(missing))


def _require_signer_config() -> None:
    missing = []
    if not _signer_service_url():
        missing.append("PORTAL_SIGNER_SERVICE_URL")
    if not _signer_api_key():
        missing.append("PORTAL_SIGNER_API_KEY")
    if not _gcs_bucket():
        missing.append("GCS_BUCKET_NAME")
    if missing:
        raise PortalError("config_missing", "Missing signer configuration", status=500, detail=", ".join(missing))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _sanitize_filename(filename: str) -> str:
    cleaned = os.path.basename(filename or "").strip()
    cleaned = cleaned.replace("\x00", "")
    cleaned = SAFE_FILENAME_PATTERN.sub("_", cleaned)
    cleaned = cleaned.replace("..", "_")
    cleaned = cleaned.strip("._")
    if not cleaned:
        raise PortalError("invalid_filename", "Invalid file name", status=400)
    return cleaned


def _build_object_name(request_id: str, safe_filename: str) -> str:
    date_prefix = _utcnow().strftime("%Y-%m-%d")
    random_suffix = secrets.token_hex(6)
    return f"upload-portal/{request_id}/{date_prefix}/{random_suffix}_{safe_filename}"


def _validate_object_name(object_name: str) -> None:
    if ".." in object_name or object_name.startswith("/"):
        raise PortalError("invalid_object_path", "Invalid object path", status=400)
    if not OBJECT_NAME_PATTERN.match(object_name):
        raise PortalError("invalid_object_path", "Invalid object path", status=400)


def _send_magic_link_email(email: str, token: str, expires_at: datetime) -> None:
    from_email = _get_env("FROM_EMAIL")
    api_key = _get_env("SENDGRID_API_KEY")
    if not from_email or not api_key:
        raise PortalError("config_missing", "Email configuration missing", status=500)

    portal_url = _portal_base_url().rstrip("/")
    link = f"{portal_url}/?upload_token={quote(token)}"
    expiration = expires_at.strftime("%Y-%m-%d %H:%M UTC")
    subject = "Secure upload link"
    plain_text = (
        "Your secure upload link is ready.\n\n"
        f"Link: {link}\n"
        f"Expires: {expiration}\n\n"
        "If you did not request this link, you can ignore this email."
    )
    html_content = f"""
    <html>
      <body style="margin:0;padding:0;background:#252a34;font-family:Raleway,Arial,sans-serif;color:#f5f7ff;">
        <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:#252a34;padding:24px;">
          <tr>
            <td align="center">
              <table width="560" cellpadding="0" cellspacing="0" role="presentation" style="background:#2c323e;border-radius:16px;padding:28px;border:1px solid rgba(255,255,255,0.08);">
                <tr>
                  <td style="font-size:18px;font-weight:600;">Secure Upload Link</td>
                </tr>
                <tr>
                  <td style="padding-top:12px;font-size:14px;line-height:1.6;color:#c8cfdd;">
                    Use the secure link below to upload your documents. This link expires on
                    <strong>{expiration}</strong>.
                  </td>
                </tr>
                <tr>
                  <td style="padding-top:18px;">
                    <a href="{link}" style="background:#00cfc8;color:#102a2f;text-decoration:none;padding:12px 18px;border-radius:999px;font-weight:600;display:inline-block;">
                      Upload Documents
                    </a>
                  </td>
                </tr>
                <tr>
                  <td style="padding-top:16px;font-size:12px;color:#9aa4b6;">
                    If you did not request this link, you can ignore this email.
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """.strip()

    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import ClickTracking, Mail, TrackingSettings

    message = Mail(
        from_email=from_email,
        to_emails=email,
        subject=subject,
        plain_text_content=plain_text,
        html_content=html_content,
    )
    message.tracking_settings = TrackingSettings()
    message.tracking_settings.click_tracking = ClickTracking(enable=False, enable_text=False)
    try:
        SendGridAPIClient(api_key=api_key).send(message)
    except Exception as exc:
        _log_event("portal_email_failed")
        raise PortalError("email_failed", "Unable to send email", status=502) from exc


def _get_request_by_token(db, token_hash: str) -> Optional[UploadPortalRequest]:
    return db.query(UploadPortalRequest).filter(UploadPortalRequest.token_hash == token_hash).first()


def _get_session_by_token(db, token_hash: str) -> Optional[UploadPortalSession]:
    return db.query(UploadPortalSession).filter(UploadPortalSession.token_hash == token_hash).first()


def create_upload_request(requester_email: str, request_ip: Optional[str] = None) -> Dict[str, Any]:
    _require_request_config()
    normalized_email = normalize_email(requester_email)
    if not normalized_email:
        raise PortalError("invalid_email", "Email is required", status=400)

    token = _generate_token()
    token_hash = _hash_token(token)
    now = _utcnow()
    expires_at = now + timedelta(minutes=_token_ttl_minutes())

    db = SessionLocal()
    try:
        request_row = UploadPortalRequest(
            requester_email=normalized_email,
            token_hash=token_hash,
            created_at=now,
            expires_at=expires_at,
            used_at=None,
            request_ip=request_ip,
        )
        db.add(request_row)
        db.commit()
        db.refresh(request_row)
    except Exception:
        db.rollback()
        _log_event("portal_request_db_failed", requester_email=normalized_email)
        raise PortalError("db_write_failed", "Unable to create upload request", status=500)
    finally:
        db.close()

    try:
        _send_magic_link_email(normalized_email, token, expires_at)
    except PortalError:
        _log_event("portal_request_email_failed", requester_email=normalized_email, request_id=request_row.id)
        raise

    _log_event("portal_request_created", requester_email=normalized_email, request_id=request_row.id)
    return {"request_id": str(request_row.id), "expires_at": expires_at.isoformat()}


def verify_upload_token(raw_token: str) -> Dict[str, Any]:
    if not raw_token:
        raise PortalError("invalid_token", "Token is required", status=400)

    token_hash = _hash_token(raw_token)
    now = _utcnow()
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    session_expires_at: Optional[str] = None
    session_token: Optional[str] = None

    db = SessionLocal()
    try:
        request_row = _get_request_by_token(db, token_hash)
        if not request_row:
            _log_event("portal_token_invalid")
            raise PortalError("invalid_token", "Upload link is invalid", status=404)
        request_id = str(request_row.id)
        if request_row.used_at:
            _log_event("portal_token_used", request_id=request_id)
            raise PortalError("token_used", "Upload link already used", status=409)
        if request_row.expires_at and request_row.expires_at < now:
            _log_event("portal_token_expired", request_id=request_id)
            raise PortalError("token_expired", "Upload link expired", status=410)

        request_row.used_at = now
        session_token = _generate_token()
        session_hash = _hash_token(session_token)
        session_expires = now + timedelta(minutes=_session_ttl_minutes())
        session_row = UploadPortalSession(
            request_id=request_row.id,
            token_hash=session_hash,
            created_at=now,
            expires_at=session_expires,
            last_used_at=now,
        )
        db.add(session_row)
        db.commit()
        db.refresh(session_row)
        session_id = str(session_row.id)
        session_expires_at = session_expires.isoformat()
    except PortalError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        _log_event("portal_verify_failed", request_id=request_id, session_id=session_id)
        raise PortalError("db_write_failed", "Unable to verify link", status=500)
    finally:
        db.close()

    _log_event("portal_verified", request_id=request_id, session_id=session_id)
    return {
        "session_token": session_token,
        "session_expires_at": session_expires_at,
        "request_id": request_id,
    }


def _load_session(raw_session_token: str) -> Dict[str, Any]:
    if not raw_session_token:
        raise PortalError("invalid_session", "Session token required", status=401)
    token_hash = _hash_token(raw_session_token)
    now = _utcnow()

    db = SessionLocal()
    try:
        session_row = _get_session_by_token(db, token_hash)
        if not session_row:
            _log_event("portal_session_invalid")
            raise PortalError("invalid_session", "Session expired", status=401)
        if session_row.expires_at and session_row.expires_at < now:
            _log_event("portal_session_expired", session_id=session_row.id)
            raise PortalError("session_expired", "Session expired", status=401)
        request_row = db.query(UploadPortalRequest).filter(UploadPortalRequest.id == session_row.request_id).first()
        if not request_row:
            _log_event("portal_request_missing", session_id=session_row.id)
            raise PortalError("invalid_session", "Session invalid", status=401)
        session_id = session_row.id
        request_id = request_row.id
        requester_email = request_row.requester_email
        session_row.last_used_at = now
        db.commit()
        db.refresh(session_row)
        return {
            "session_id": session_id,
            "request_id": request_id,
            "requester_email": requester_email,
        }
    except PortalError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        _log_event("portal_session_load_failed")
        raise PortalError("db_read_failed", "Unable to load session", status=500)
    finally:
        db.close()


def _call_signer_service(object_name: str, content_type: str) -> str:
    url = _signer_service_url()
    api_key = _signer_api_key()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "bucket": _gcs_bucket(),
        "object_name": object_name,
        "content_type": content_type,
        "method": "PUT",
    }
    response = requests.post(url, headers=headers, json=payload, timeout=15)
    if response.status_code >= 300:
        _log_event("portal_signer_failed")
        raise PortalError("signer_failed", "Unable to sign upload", status=502)
    data = response.json()
    signed_url = data.get("signed_url") or data.get("signedUrl") or data.get("url")
    if not signed_url:
        _log_event("portal_signer_missing")
        raise PortalError("signer_failed", "Signed URL missing", status=502)
    return signed_url


def create_signed_upload_url(raw_session_token: str, original_filename: str,
                             content_type: Optional[str], byte_size: Optional[int]) -> Dict[str, Any]:
    _require_signer_config()
    if byte_size is None:
        raise PortalError("invalid_byte_size", "File size is required", status=400)
    if not isinstance(byte_size, int) or byte_size < 0:
        raise PortalError("invalid_byte_size", "File size is invalid", status=400)
    if byte_size > _max_file_size_bytes():
        raise PortalError("file_too_large", "File exceeds size limit", status=413)
    if not content_type or content_type.lower().strip() not in _allowed_content_types():
        raise PortalError("invalid_content_type", "Unsupported content type", status=400)

    session_data = _load_session(raw_session_token)
    request_id = session_data["request_id"]
    session_id = session_data["session_id"]
    safe_filename = _sanitize_filename(original_filename)
    object_name = _build_object_name(str(request_id), safe_filename)
    _validate_object_name(object_name)
    signed_url = _call_signer_service(object_name, content_type or "application/octet-stream")

    db = SessionLocal()
    try:
        file_row = UploadPortalFile(
            request_id=request_id,
            session_id=session_id,
            user_id=None,
            user_email=None,
            gcs_bucket=_gcs_bucket(),
            object_name=object_name,
            original_filename=safe_filename,
            content_type=content_type,
            byte_size=byte_size,
            created_at=_utcnow(),
            completed_at=None,
        )
        db.add(file_row)
        db.commit()
        db.refresh(file_row)
    except Exception:
        db.rollback()
        _log_event("portal_file_record_failed", request_id=request_id, session_id=session_id)
        raise PortalError("db_write_failed", "Unable to create upload record", status=500)
    finally:
        db.close()

    _log_event("portal_signed_url_issued", request_id=request_id, session_id=session_id, byte_size=byte_size)
    return {"upload_id": str(file_row.id), "signed_url": signed_url}


def complete_upload(raw_session_token: str, upload_id: str) -> Dict[str, Any]:
    session_data = _load_session(raw_session_token)
    request_id = session_data["request_id"]
    session_id = session_data["session_id"]

    normalized_email = normalize_email(session_data["requester_email"])
    db = SessionLocal()
    try:
        file_row = (
            db.query(UploadPortalFile)
            .filter(UploadPortalFile.id == upload_id, UploadPortalFile.session_id == session_id)
            .first()
        )
        if not file_row:
            raise PortalError("upload_not_found", "Upload record not found", status=404)
        if file_row.completed_at:
            return {"upload_id": str(file_row.id), "status": "already_completed"}

        user = (
            db.query(User)
            .filter(func.lower(User.email) == normalized_email)
            .first()
        )
        if not user:
            _log_event("portal_unknown_user", requester_email=normalized_email, request_id=request_id)
            raise PortalError("unknown_user_email", "No matching user found", status=404)

        file_row.user_id = user.id
        file_row.user_email = normalized_email
        file_row.completed_at = _utcnow()
        db.commit()
    except PortalError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        _log_event("portal_complete_failed", request_id=request_id)
        raise PortalError("db_write_failed", "Unable to finalize upload", status=500)
    finally:
        db.close()

    _log_event("portal_upload_completed", request_id=request_id, session_id=session_id)
    return {"upload_id": upload_id, "status": "completed"}


def list_recent_uploads(limit: int = 25) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        rows = (
            db.query(UploadPortalFile)
            .order_by(UploadPortalFile.created_at.desc())
            .limit(limit)
            .all()
        )
        items = [
            {
                "id": str(row.id),
                "request_id": str(row.request_id),
                "session_id": str(row.session_id),
                "user_id": row.user_id,
                "user_email": row.user_email,
                "gcs_bucket": row.gcs_bucket,
                "content_type": row.content_type,
                "byte_size": row.byte_size,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            }
            for row in rows
        ]
        return {"items": items}
    except Exception:
        _log_event("portal_list_failed")
        raise PortalError("db_read_failed", "Unable to list uploads", status=500)
    finally:
        db.close()
