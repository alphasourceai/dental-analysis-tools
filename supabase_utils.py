import logging
import os
from datetime import datetime
from uuid import UUID, uuid4

import requests
from supabase import create_client

from database import SessionLocal
from models import AdminUser, UploadFile

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

_admin_client = None
_auth_client = None
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


def _get_supabase_auth_key():
    return SUPABASE_ANON_KEY or SUPABASE_SERVICE_ROLE_KEY


def _supabase_auth_headers(access_token=None):
    auth_key = _get_supabase_auth_key()
    if not SUPABASE_URL or not auth_key:
        return None
    return {
        "apikey": auth_key,
        "Authorization": f"Bearer {access_token or auth_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _auth_error_message(response):
    body = (response.text or "").replace("\n", " ").strip()
    return f"status {response.status_code} body {body[:500]}"


def send_admin_password_reset(email, redirect_to):
    normalized_email = (email or "").strip().lower()
    if not normalized_email:
        return False, "Email is required"
    if not redirect_to:
        return False, "Password reset redirect URL is not configured"

    headers = _supabase_auth_headers()
    if not headers:
        return False, "Supabase auth is not configured"

    url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/recover"
    try:
        response = requests.post(
            url,
            headers=headers,
            params={"redirect_to": redirect_to},
            json={"email": normalized_email},
            timeout=8,
        )
    except requests.RequestException as exc:
        logging.error("[auth] password reset request failed email=%s err=%s", normalized_email, str(exc))
        return False, "Unable to contact Supabase Auth"

    if response.status_code not in (200, 201, 204):
        message = _auth_error_message(response)
        logging.error("[auth] password reset request bad status email=%s %s", normalized_email, message)
        return False, message
    logging.info("[auth] password reset requested email=%s redirect_to=%s", normalized_email, redirect_to)
    return True, None


def verify_password_recovery_token(token_hash):
    token_hash = (token_hash or "").strip()
    if not token_hash:
        return None, "Recovery token is missing"

    headers = _supabase_auth_headers()
    if not headers:
        return None, "Supabase auth is not configured"

    url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/verify"
    try:
        response = requests.post(
            url,
            headers=headers,
            json={"token_hash": token_hash, "type": "recovery"},
            timeout=8,
        )
    except requests.RequestException as exc:
        logging.error("[auth] password recovery verify failed err=%s", str(exc))
        return None, "Unable to contact Supabase Auth"

    if response.status_code not in (200, 201):
        message = _auth_error_message(response)
        logging.error("[auth] password recovery verify bad status %s", message)
        return None, message

    try:
        payload = response.json()
    except ValueError:
        return None, "Invalid Supabase Auth response"

    session = payload.get("session") or payload
    user = payload.get("user") or session.get("user") or {}
    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    if not access_token:
        return None, "Recovery session is missing an access token"

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": {"id": user.get("id"), "email": user.get("email")},
    }, None


def update_password_with_recovery_token(access_token, new_password):
    if not access_token:
        return False, "Recovery session is missing"
    if not new_password:
        return False, "Password is required"

    headers = _supabase_auth_headers(access_token=access_token)
    if not headers:
        return False, "Supabase auth is not configured"

    url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/user"
    try:
        response = requests.put(
            url,
            headers=headers,
            json={"password": new_password},
            timeout=8,
        )
    except requests.RequestException as exc:
        logging.error("[auth] password update failed err=%s", str(exc))
        return False, "Unable to contact Supabase Auth"

    if response.status_code not in (200, 201):
        message = _auth_error_message(response)
        logging.error("[auth] password update bad status %s", message)
        return False, message
    logging.info("[auth] password updated via recovery token")
    return True, None


def persist_upload_file(file_bytes, user_email, tool_name, original_filename, content_type=None, upload_id=None):
    client = _get_supabase_admin_client()
    if not client:
        return None

    date_prefix = datetime.utcnow().strftime("%Y-%m-%d")
    unique_name = f"{uuid4()}_{original_filename}"
    bucket = "consulting-uploads"
    object_path = f"{user_email}/{date_prefix}/{tool_name}/{unique_name}"

    try:
        client.storage.from_(bucket).upload(
            object_path,
            file_bytes,
            {"content-type": content_type, "upsert": False},
        )
        logging.info(
            "Supabase Storage upload succeeded: %s -> %s/%s",
            original_filename,
            bucket,
            object_path,
        )
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
                bucket=bucket,
                object_path=object_path,
            )
        )
        logging.info(
            "upload_files record pending: bucket=%s object_path=%s upload_id=%s",
            bucket,
            object_path,
            normalized_upload_id,
        )
        db.commit()
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
        admin_user = db.query(AdminUser).filter(AdminUser.user_id == normalized_user_id).first()
        is_admin = bool(admin_user and (admin_user.role or "").strip().lower() == "admin")
        logging.info("[auth] admin_users db check user_id=%s result=%s", user_id, is_admin)
        if is_admin:
            return True
    except Exception as exc:
        logging.error("[auth] admin_users db check failed user_id=%s err=%s", user_id, str(exc))
    finally:
        db.close()

    if not SUPABASE_URL:
        logging.error("[auth] admin_users rest check missing SUPABASE_URL user_id=%s", user_id)
        return False
    rest_key = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY
    if not rest_key:
        logging.error("[auth] admin_users rest check missing key user_id=%s", user_id)
        return False
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/admin_users"
    headers = {
        "apikey": rest_key,
        "Authorization": f"Bearer {rest_key}",
        "Accept": "application/json",
    }
    params = {
        "select": "user_id",
        "user_id": f"eq.{normalized_user_id}",
        "role": "eq.admin",
        "limit": "1",
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=3)
    except requests.RequestException as exc:
        logging.error("[auth] admin_users rest check failed user_id=%s err=%s", user_id, str(exc))
        return False
    if response.status_code != 200:
        body = (response.text or "").replace("\n", " ")
        truncated = body[:500]
        logging.error(
            "[auth] admin_users rest check bad status user_id=%s status=%s params=%s body=%s",
            user_id,
            response.status_code,
            params,
            truncated,
        )
        return False
    try:
        payload = response.json()
    except ValueError as exc:
        logging.error("[auth] admin_users rest check invalid json user_id=%s err=%s", user_id, str(exc))
        return False
    is_admin = bool(payload)
    logging.info("[auth] admin_users rest check user_id=%s result=%s", user_id, is_admin)
    return is_admin


def get_admin_user_count():
    db = SessionLocal()
    try:
        return db.query(AdminUser).count()
    except Exception as exc:
        logging.error(f"Error counting admin_users: {str(exc)}")
        return None
    finally:
        db.close()
