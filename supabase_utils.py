import logging
import os
from datetime import datetime
from uuid import UUID, uuid4

from supabase import create_client

from database import SessionLocal
from models import AdminUser, UploadFile

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

REQUIRED_BUCKETS = (
    "consulting-uploads",
    "consulting-exports",
    "consulting-admin-exports",
)

_admin_client = None
_auth_client = None
_buckets_checked = False


def _get_supabase_admin_client():
    global _admin_client
    if _admin_client is not None:
        return _admin_client
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        logging.error("Supabase admin client is not configured")
        return None
    _admin_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _admin_client


def _get_supabase_auth_client():
    global _auth_client
    if _auth_client is not None:
        return _auth_client
    if not SUPABASE_URL:
        logging.error("Supabase URL is not configured")
        return None
    auth_key = SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY
    if not auth_key:
        logging.error("Supabase auth client key is not configured")
        return None
    _auth_client = create_client(SUPABASE_URL, auth_key)
    return _auth_client


def _extract_attr(response, key):
    if hasattr(response, key):
        return getattr(response, key)
    if isinstance(response, dict):
        return response.get(key)
    return None


def _normalize_uuid(value):
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _extract_error_payload(exc):
    if isinstance(exc, dict):
        return exc
    if getattr(exc, "args", None):
        for arg in exc.args:
            if isinstance(arg, dict):
                return arg
    return None


def _extract_error_status(payload):
    if not payload:
        return None
    for key in ("statusCode", "status_code", "status"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    return None


def _extract_error_message(payload):
    if not payload:
        return ""
    for key in ("message", "error", "msg", "details"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def _is_bucket_already_exists_error(exc):
    payload = _extract_error_payload(exc)
    status_code = _extract_error_status(payload)
    message = _extract_error_message(payload)
    if status_code is None:
        status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if not message:
        message = str(exc)
    if status_code == 409:
        return True
    return "already exists" in message.lower()


def ensure_bucket_exists(bucket_name):
    client = _get_supabase_admin_client()
    if not client:
        return False
    try:
        client.storage.create_bucket(bucket_name, options={"public": False})
        logging.info("Supabase bucket ensured (created): %s", bucket_name)
        return True
    except Exception as exc:
        if _is_bucket_already_exists_error(exc):
            logging.info("Supabase bucket ensured (exists): %s", bucket_name)
            return True
        logging.error("Supabase bucket creation failed for %s: %s", bucket_name, str(exc))
        return False


def _ensure_storage_buckets():
    global _buckets_checked
    if _buckets_checked:
        return
    client = _get_supabase_admin_client()
    if not client:
        return
    results = [ensure_bucket_exists(bucket_name) for bucket_name in REQUIRED_BUCKETS]
    if all(results):
        _buckets_checked = True
        logging.info("Supabase bucket initialization complete")
    else:
        logging.error("Supabase bucket initialization incomplete")


def persist_upload_file(file_bytes, user_email, tool_name, original_filename, content_type=None, upload_id=None):
    client = _get_supabase_admin_client()
    if not client:
        return None

    _ensure_storage_buckets()

    date_prefix = datetime.utcnow().strftime("%Y-%m-%d")
    unique_name = f"{uuid4()}_{original_filename}"
    storage_path = f"consulting-uploads/{user_email}/{date_prefix}/{tool_name}/{unique_name}"

    try:
        logging.info("Supabase Storage upload attempt: %s -> %s", original_filename, storage_path)
        client.storage.from_("consulting-uploads").upload(
            storage_path,
            file_bytes,
            {"content-type": content_type, "upsert": False},
        )
        logging.info("Supabase Storage upload succeeded: %s -> %s", original_filename, storage_path)
    except Exception as exc:
        logging.error(f"Supabase Storage upload failed for {original_filename}: {str(exc)}")
        return None

    upload_file_id = uuid4()
    normalized_upload_id = _normalize_uuid(upload_id)
    db = SessionLocal()
    try:
        db.add(
            UploadFile(
                id=upload_file_id,
                upload_id=normalized_upload_id,
                user_email=user_email,
                tool_name=tool_name,
                original_filename=original_filename,
                content_type=content_type,
                byte_size=len(file_bytes) if file_bytes is not None else None,
                bucket="consulting-uploads",
                storage_path=storage_path,
            )
        )
        db.commit()
        logging.info("Supabase upload metadata saved: %s", upload_file_id)
        return upload_file_id
    except Exception as exc:
        logging.error(f"Error saving upload_files record for {original_filename}: {str(exc)}")
        db.rollback()
        return None
    finally:
        db.close()


def update_upload_file_upload_id(upload_file_id, upload_id):
    normalized_upload_id = _normalize_uuid(upload_id)
    if not upload_file_id or not normalized_upload_id:
        if upload_file_id and upload_id:
            logging.error(f"Upload ID {upload_id} is not a valid UUID for upload_files update")
        return

    db = SessionLocal()
    try:
        db.query(UploadFile).filter(UploadFile.id == upload_file_id).update(
            {"upload_id": normalized_upload_id}
        )
        db.commit()
    except Exception as exc:
        logging.error(f"Error updating upload_files record {upload_file_id}: {str(exc)}")
        db.rollback()
    finally:
        db.close()


def sign_in_admin(email, password):
    client = _get_supabase_auth_client()
    if not client:
        return None, "Supabase auth is not configured"
    try:
        response = client.auth.sign_in_with_password({"email": email, "password": password})
        session = _extract_attr(response, "session")
        user = _extract_attr(response, "user")
        if not session or not user:
            return None, "Invalid auth response"
        access_token = _extract_attr(session, "access_token")
        refresh_token = _extract_attr(session, "refresh_token")
        user_id = _extract_attr(user, "id")
        user_email = _extract_attr(user, "email")
        if not access_token or not user_id:
            return None, "Missing auth session data"
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user": {"id": user_id, "email": user_email},
        }, None
    except Exception as exc:
        return None, str(exc)


def get_current_admin_user(access_token):
    if not access_token:
        return None
    client = _get_supabase_auth_client()
    if not client:
        return None
    try:
        try:
            response = client.auth.get_user(access_token)
        except TypeError:
            response = client.auth.get_user(jwt=access_token)
        user = _extract_attr(response, "user")
        if not user:
            return None
        return {"id": _extract_attr(user, "id"), "email": _extract_attr(user, "email")}
    except Exception as exc:
        logging.error(f"Error fetching Supabase user: {str(exc)}")
        return None


def is_admin_user(user_id):
    normalized_user_id = _normalize_uuid(user_id)
    if not normalized_user_id:
        return False
    db = SessionLocal()
    try:
        return (
            db.query(AdminUser)
            .filter(AdminUser.user_id == normalized_user_id, AdminUser.role == "admin")
            .first()
            is not None
        )
    except Exception as exc:
        logging.error(f"Error checking admin_users for {user_id}: {str(exc)}")
        return False
    finally:
        db.close()


def get_admin_user_count():
    db = SessionLocal()
    try:
        return db.query(AdminUser).count()
    except Exception as exc:
        logging.error(f"Error counting admin_users: {str(exc)}")
        return None
    finally:
        db.close()
