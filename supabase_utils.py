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
ADMIN_ACCESS_ROLES = {"admin", "super_admin"}
ADMIN_AUTH_LIST_PAGE_SIZE = 100
ADMIN_AUTH_LIST_MAX_PAGES = 50

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


def _normalize_email(value):
    return (value or "").strip().lower()


def _safe_auth_error(status, code, message):
    return {"status": status, "code": code, "message": message}


def _auth_admin_api():
    client = _get_supabase_admin_client()
    if not client:
        return None, _safe_auth_error(
            500,
            "supabase_auth_not_configured",
            "Supabase Auth admin access is not configured.",
        )
    auth = getattr(client, "auth", None)
    admin_api = getattr(auth, "admin", None)
    if not admin_api:
        return None, _safe_auth_error(
            500,
            "supabase_auth_admin_unavailable",
            "Supabase Auth admin access is unavailable.",
        )
    return admin_api, None


def _auth_user_payload(user, normalized_email):
    if not user:
        return None
    user_id = _extract_attr(user, "id")
    user_email = _normalize_email(_extract_attr(user, "email") or normalized_email)
    if not user_id:
        return None
    return {
        "user_id": str(user_id),
        "email": user_email,
    }


def _auth_users_from_response(response):
    if response is None:
        return []
    if isinstance(response, list):
        return response
    users = _extract_attr(response, "users")
    if isinstance(users, list):
        return users
    data = _extract_attr(response, "data")
    if isinstance(data, list):
        return data
    if hasattr(data, "users"):
        return getattr(data, "users") or []
    return []


def find_supabase_auth_user_by_email(email):
    normalized_email = _normalize_email(email)
    if not normalized_email:
        return None, _safe_auth_error(400, "invalid_email", "Email is required.")

    admin_api, error = _auth_admin_api()
    if error:
        return None, error

    direct_lookup = getattr(admin_api, "get_user_by_email", None)
    if callable(direct_lookup):
        try:
            response = direct_lookup(normalized_email)
            user = _extract_attr(response, "user") or response
            payload = _auth_user_payload(user, normalized_email)
            if payload and payload["email"] == normalized_email:
                return payload, None
        except Exception as exc:
            logging.warning("[auth] direct Supabase Auth user email lookup failed err=%s", str(exc))

    list_users = getattr(admin_api, "list_users", None)
    if not callable(list_users):
        return None, _safe_auth_error(
            500,
            "supabase_auth_lookup_unavailable",
            "Supabase Auth user lookup is unavailable.",
        )

    try:
        for page in range(1, ADMIN_AUTH_LIST_MAX_PAGES + 1):
            response = list_users(page=page, per_page=ADMIN_AUTH_LIST_PAGE_SIZE)
            users = _auth_users_from_response(response)
            for user in users:
                payload = _auth_user_payload(user, normalized_email)
                if payload and payload["email"] == normalized_email:
                    return payload, None
            if len(users) < ADMIN_AUTH_LIST_PAGE_SIZE:
                return None, None
    except Exception as exc:
        logging.error("[auth] Supabase Auth user lookup failed err=%s", str(exc))
        return None, _safe_auth_error(
            502,
            "supabase_auth_lookup_failed",
            "Unable to look up Supabase Auth user.",
        )

    return None, _safe_auth_error(
        500,
        "supabase_auth_lookup_limit_exceeded",
        "Unable to confirm Supabase Auth user by email.",
    )


def invite_supabase_auth_user_by_email(email):
    normalized_email = _normalize_email(email)
    if not normalized_email:
        return None, _safe_auth_error(400, "invalid_email", "Email is required.")

    redirect_to = os.getenv("ADMIN_INVITE_REDIRECT_URL", "").strip()
    if not redirect_to:
        return None, _safe_auth_error(
            500,
            "admin_invite_redirect_missing",
            "Admin invite redirect URL is not configured.",
        )

    admin_api, error = _auth_admin_api()
    if error:
        return None, error

    invite_user = getattr(admin_api, "invite_user_by_email", None)
    if not callable(invite_user):
        return None, _safe_auth_error(
            500,
            "supabase_auth_invite_unavailable",
            "Supabase Auth invite is unavailable.",
        )

    try:
        response = invite_user(normalized_email, {"redirect_to": redirect_to})
    except Exception as exc:
        logging.error("[auth] Supabase Auth invite failed err=%s", str(exc))
        return None, _safe_auth_error(
            502,
            "supabase_auth_invite_failed",
            "Unable to send Supabase Auth invite.",
        )

    user = _extract_attr(response, "user") or response
    payload = _auth_user_payload(user, normalized_email)
    if not payload:
        logging.error("[auth] Supabase Auth invite response missing user id")
        return None, _safe_auth_error(
            502,
            "supabase_auth_invite_invalid_response",
            "Supabase Auth invite response was invalid.",
        )
    return payload, None


def resolve_admin_auth_user_by_email(email):
    normalized_email = _normalize_email(email)
    existing_user, error = find_supabase_auth_user_by_email(normalized_email)
    if error:
        return None, error
    if existing_user:
        return {
            "user_id": existing_user["user_id"],
            "email": normalized_email,
            "invited": False,
            "existing": True,
        }, None

    invited_user, error = invite_supabase_auth_user_by_email(normalized_email)
    if error:
        return None, error
    return {
        "user_id": invited_user["user_id"],
        "email": normalized_email,
        "invited": True,
        "existing": False,
    }, None


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


def _admin_access_allows(admin_user):
    if not admin_user:
        return False
    role = (_extract_attr(admin_user, "role") or "").strip().lower()
    status = (_extract_attr(admin_user, "status") or "active").strip().lower()
    deactivated_at = _extract_attr(admin_user, "deactivated_at")
    return role in ADMIN_ACCESS_ROLES and status == "active" and not deactivated_at


def is_admin_user(user_id):
    normalized_user_id = _normalize_uuid(user_id)
    if not normalized_user_id:
        return False

    db = SessionLocal()
    try:
        admin_user = db.query(AdminUser).filter(AdminUser.user_id == normalized_user_id).first()
        is_admin = _admin_access_allows(admin_user)
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
        "select": "user_id,role,status,deactivated_at",
        "user_id": f"eq.{normalized_user_id}",
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
    is_admin = any(_admin_access_allows(row) for row in payload if isinstance(row, dict))
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
