from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import time
import uuid
from datetime import datetime
from io import BytesIO
from typing import Any, Callable, Dict, Optional

import pandas as pd
import requests
from sqlalchemy import text

from analysis_utils import (
    anthropic_analysis,
    deduplicate_issues,
    extract_text_from_pdf,
    get_model_labels,
    log_active_models,
    openai_analysis,
    parse_issues_from_analysis,
    parse_trends_from_analysis,
    send_email,
    send_followup_email,
    xai_analysis,
)
from database import SessionLocal
from models import ClientSubmission, Upload, User, update_submission_status
from supabase_utils import persist_upload_file, update_upload_file_upload_id

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".xlsx", ".csv", ".pdf"}
EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
PUBLIC_TOOL_NAME = "Financial Analyzer"
MAX_PROVIDER_RETRIES = 2
PROVIDER_RETRY_BACKOFF_SECONDS = 0.75
TRANSIENT_PROVIDER_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
PROVIDER_UNAVAILABLE_MESSAGE = (
    "Analysis unavailable from this model due to temporary provider capacity. "
    "Other model results were processed."
)
CANCELED_MESSAGE = "Analysis canceled. No results were saved."
CancelChecker = Callable[[], bool]
SubmissionCreatedCallback = Callable[[str], None]


class PublicAnalyzerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class PublicAnalyzerCanceled(Exception):
    def __init__(
        self,
        submission_id: Optional[Any] = None,
        upload_id: Optional[Any] = None,
        message: str = CANCELED_MESSAGE,
    ):
        super().__init__(message)
        self.code = "analysis_canceled"
        self.message = message
        self.submission_id = str(submission_id) if submission_id else None
        self.upload_id = str(upload_id) if upload_id else None


def submit_public_analyzer_submission(
    *,
    first_name: str,
    last_name: str,
    office_name: str,
    email: str,
    org_type: str,
    uploaded_file_bytes: bytes,
    original_filename: str,
    content_type: Optional[str] = None,
    cid: Optional[str] = None,
    source_path: Optional[str] = None,
    phone: Optional[str] = None,
    financial_only_acknowledgement: Optional[bool] = None,
    acknowledgement_timestamp: Optional[datetime] = None,
    acknowledgement_ip: Optional[str] = None,
    acknowledgement_version: Optional[str] = None,
    require_public_api_metadata: bool = False,
    cancel_checker: Optional[CancelChecker] = None,
    submission_created_callback: Optional[SubmissionCreatedCallback] = None,
) -> Dict[str, Any]:
    """Run the existing public analyzer workflow without Streamlit session state."""
    del source_path  # The current schema does not store the originating frontend path.

    submission_id: Optional[str] = None
    upload_id: Optional[str] = None
    run_id = str(uuid.uuid4())

    try:
        _raise_if_canceled(cancel_checker)
        validated = _validate_submission_input(
            first_name=first_name,
            last_name=last_name,
            office_name=office_name,
            email=email,
            org_type=org_type,
            uploaded_file_bytes=uploaded_file_bytes,
            original_filename=original_filename,
            phone=phone,
            financial_only_acknowledgement=financial_only_acknowledgement,
            require_public_api_metadata=require_public_api_metadata,
        )
        _raise_if_canceled(cancel_checker)
        normalized_email = validated["email"]
        file_name = validated["original_filename"]
        normalized_phone = validated["phone"]
        file_type = (content_type or _guess_content_type(file_name)).strip()
        file_bytes = bytes(uploaded_file_bytes)

        user_info = {
            "first_name": first_name,
            "last_name": last_name,
            "office_name": office_name,
            "email": normalized_email,
            "org_type": org_type,
            "phone": normalized_phone,
            "ghl_cid": _normalize_optional_text(cid),
            "financial_only_acknowledgement": _coerce_optional_bool(financial_only_acknowledgement),
            "acknowledgement_timestamp": acknowledgement_timestamp,
            "acknowledgement_ip": _normalize_optional_text(acknowledgement_ip),
            "acknowledgement_version": _normalize_optional_text(acknowledgement_version),
        }

        logger.info("[analysis] start run_id=%s source=public_service", run_id)
        log_active_models(run_id)

        _raise_if_canceled(cancel_checker)
        _upsert_user(user_info)
        _raise_if_canceled(cancel_checker)
        submission_id = _create_client_submission(user_info, run_id)
        _notify_submission_created(submission_created_callback, submission_id, run_id)

        _raise_if_canceled(cancel_checker)
        upload_file_id = persist_upload_file(
            file_bytes=file_bytes,
            user_email=normalized_email,
            tool_name=PUBLIC_TOOL_NAME,
            original_filename=file_name,
            content_type=file_type,
        )
        if not upload_file_id:
            raise PublicAnalyzerError("storage_failed", "Unable to persist uploaded file.")

        _raise_if_canceled(cancel_checker)
        data_input = _extract_data_input(file_bytes, file_name)
        _raise_if_canceled(cancel_checker)
        results = _run_public_analysis(data_input, cancel_checker=cancel_checker)

        # After this point, external email side effects may begin. To avoid a
        # half-sent/half-saved result, cancellation is no longer observed.
        _raise_if_canceled(cancel_checker)
        emails_sent = _send_public_emails(user_info, file_bytes, file_name, file_type, results)

        upload_id = _create_upload_record(
            user_email=normalized_email,
            file_name=file_name,
            tool_name=PUBLIC_TOOL_NAME,
            results=results,
        )
        update_upload_file_upload_id(upload_file_id, upload_id)

        if emails_sent:
            _mark_submission_completed(submission_id, upload_id, cid)
            logger.info("[analysis] finished run_id=%s source=public_service", run_id)
            return _result(
                ok=True,
                submission_id=submission_id,
                upload_id=upload_id,
                status="completed",
            )

        _mark_submission_error(submission_id, "email_failed")
        return _result(
            ok=False,
            submission_id=submission_id,
            upload_id=upload_id,
            status="error",
            error_code="email_failed",
            error_message="One or more analysis emails failed to send.",
        )
    except PublicAnalyzerCanceled as exc:
        if submission_id:
            _mark_submission_canceled(submission_id)
            exc.submission_id = str(submission_id)
        if upload_id:
            exc.upload_id = str(upload_id)
        raise
    except PublicAnalyzerError as exc:
        if submission_id:
            _mark_submission_error(submission_id, exc.code, exc.message)
        return _result(
            ok=False,
            submission_id=submission_id,
            upload_id=upload_id,
            status="error",
            error_code=exc.code,
            error_message=exc.message,
        )
    except Exception as exc:
        logger.exception("[analysis] error run_id=%s source=public_service", run_id)
        if submission_id:
            _mark_submission_error(submission_id, "analysis_failed", str(exc)[:300])
        return _result(
            ok=False,
            submission_id=submission_id,
            upload_id=upload_id,
            status="error",
            error_code="analysis_failed",
            error_message=str(exc)[:300],
        )


def _raise_if_canceled(cancel_checker: Optional[CancelChecker]) -> None:
    if not cancel_checker:
        return
    try:
        if cancel_checker():
            raise PublicAnalyzerCanceled()
    except PublicAnalyzerCanceled:
        raise
    except Exception:
        logger.warning("[analysis] cancel check failed")


def _notify_submission_created(
    callback: Optional[SubmissionCreatedCallback],
    submission_id: str,
    run_id: str,
) -> None:
    if not callback:
        return
    try:
        callback(submission_id)
    except Exception:
        logger.warning("[analysis] submission callback failed run_id=%s", run_id)


def _result(
    *,
    ok: bool,
    submission_id: Optional[Any],
    upload_id: Optional[Any],
    status: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "ok": ok,
        "submission_id": str(submission_id) if submission_id else None,
        "upload_id": str(upload_id) if upload_id else None,
        "status": status,
        "error_code": error_code,
        "error_message": error_message,
    }


def _validate_submission_input(
    *,
    first_name: str,
    last_name: str,
    office_name: str,
    email: str,
    org_type: str,
    uploaded_file_bytes: bytes,
    original_filename: str,
    phone: Optional[str] = None,
    financial_only_acknowledgement: Optional[bool] = None,
    require_public_api_metadata: bool = False,
) -> Dict[str, Any]:
    if not first_name:
        raise PublicAnalyzerError("validation_error", "First name is required.")
    if not last_name:
        raise PublicAnalyzerError("validation_error", "Last name is required.")
    if not office_name:
        raise PublicAnalyzerError("validation_error", "Office/Group name is required.")
    if not org_type:
        raise PublicAnalyzerError("validation_error", "Type is required.")
    if org_type not in ("Location", "Group"):
        raise PublicAnalyzerError("validation_error", "Type must be Location or Group.")

    normalized_email = normalize_email(email)
    if not normalized_email:
        raise PublicAnalyzerError("validation_error", "Email address is required.")
    if not EMAIL_PATTERN.match(normalized_email):
        raise PublicAnalyzerError("validation_error", "Please enter a valid email address.")

    if uploaded_file_bytes is None:
        raise PublicAnalyzerError("validation_error", "Uploaded file is required.")
    if not isinstance(uploaded_file_bytes, (bytes, bytearray, memoryview)):
        raise PublicAnalyzerError("validation_error", "Uploaded file bytes are invalid.")
    filename = (original_filename or "").strip()
    if not filename:
        raise PublicAnalyzerError("validation_error", "Original filename is required.")

    extension = _file_extension(filename)
    if extension not in ALLOWED_EXTENSIONS:
        raise PublicAnalyzerError("unsupported_file_type", "Unsupported file type.")

    normalized_phone = _normalize_optional_text(phone)
    if require_public_api_metadata and not normalized_phone:
        raise PublicAnalyzerError("validation_error", "Phone number is required.")
    if require_public_api_metadata and _coerce_optional_bool(financial_only_acknowledgement) is not True:
        raise PublicAnalyzerError(
            "validation_error",
            "Financial/practice operations acknowledgement is required.",
        )

    return {
        "email": normalized_email,
        "original_filename": filename,
        "phone": normalized_phone,
    }


def normalize_email(raw_email: str) -> str:
    if not raw_email:
        return ""
    return raw_email.strip().lower()


def _normalize_optional_text(value: Optional[Any]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _coerce_optional_bool(value: Optional[Any]) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
    return bool(value)


def _file_extension(filename: str) -> str:
    dot_index = filename.rfind(".")
    if dot_index == -1:
        return ""
    return filename[dot_index:].lower()


def _guess_content_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _bytes_file(file_bytes: bytes, filename: str) -> BytesIO:
    file_obj = BytesIO(file_bytes)
    file_obj.name = filename
    return file_obj


def _extract_data_input(file_bytes: bytes, filename: str) -> str:
    extension = _file_extension(filename)
    file_obj = _bytes_file(file_bytes, filename)
    if extension == ".pdf":
        return extract_text_from_pdf(file_obj)
    if extension == ".csv":
        return pd.read_csv(file_obj).to_string(index=False)
    return pd.read_excel(file_obj).to_string(index=False)


def _run_public_analysis(
    data_input: str,
    *,
    cancel_checker: Optional[CancelChecker] = None,
) -> Dict[str, Any]:
    model_labels = get_model_labels()
    provider_specs = [
        ("openai", "OpenAI Analysis", "openai", openai_analysis),
        ("xai", "xAI Analysis", "xai", xai_analysis),
        ("anthropic", "AnthropicAI Analysis", "anthropic", anthropic_analysis),
    ]
    raw_analyses: Dict[str, str] = {}
    parsed_issues: Dict[str, Any] = {}
    parsed_trends: Dict[str, Any] = {}
    successful_provider_count = 0

    for provider_name, result_key, label_key, analysis_func in provider_specs:
        _raise_if_canceled(cancel_checker)
        analysis_text, succeeded = _run_provider_analysis_with_retry(
            provider_name=provider_name,
            analysis_func=analysis_func,
            data_input=data_input,
            cancel_checker=cancel_checker,
        )
        _raise_if_canceled(cancel_checker)
        raw_analyses[result_key] = analysis_text
        parsed_issues[label_key] = parse_issues_from_analysis(analysis_text, model_labels[label_key])
        parsed_trends[label_key] = parse_trends_from_analysis(analysis_text, model_labels[label_key])
        if succeeded:
            successful_provider_count += 1

    if successful_provider_count == 0:
        raise PublicAnalyzerError(
            "provider_unavailable",
            "The analyzer providers are temporarily unavailable. Please try again later.",
        )

    all_issues = parsed_issues["openai"] + parsed_issues["xai"] + parsed_issues["anthropic"]
    all_trends = parsed_trends["openai"] + parsed_trends["xai"] + parsed_trends["anthropic"]
    deduplicated_issues = deduplicate_issues(all_issues)

    return {
        "raw_analyses": raw_analyses,
        "parsed_issues": parsed_issues,
        "parsed_trends": parsed_trends,
        "all_trends": all_trends,
        "deduplicated_issues": deduplicated_issues,
        "total_issue_count": len(deduplicated_issues),
    }


def _run_provider_analysis_with_retry(
    *,
    provider_name: str,
    analysis_func: Any,
    data_input: str,
    cancel_checker: Optional[CancelChecker] = None,
) -> tuple[str, bool]:
    for attempt in range(1, MAX_PROVIDER_RETRIES + 2):
        _raise_if_canceled(cancel_checker)
        try:
            analysis_text = analysis_func(data_input)
            if not isinstance(analysis_text, str) or not analysis_text.strip():
                raise ValueError("empty_provider_response")
            if attempt > 1:
                logger.info(
                    "[analysis] provider recovered provider=%s attempt=%s",
                    provider_name,
                    attempt,
                )
            return analysis_text, True
        except Exception as exc:
            transient = _is_transient_provider_error(exc)
            logger.warning(
                "[analysis] provider failed provider=%s attempt=%s transient=%s error_type=%s",
                provider_name,
                attempt,
                transient,
                _safe_provider_error_type(exc),
            )
            if not transient or attempt > MAX_PROVIDER_RETRIES:
                break
            _raise_if_canceled(cancel_checker)
            time.sleep(PROVIDER_RETRY_BACKOFF_SECONDS * attempt)

    logger.warning("[analysis] provider unavailable provider=%s", provider_name)
    return PROVIDER_UNAVAILABLE_MESSAGE, False


def _is_transient_provider_error(exc: Exception) -> bool:
    status_code = _provider_status_code(exc)
    if status_code in TRANSIENT_PROVIDER_STATUS_CODES:
        return True

    safe_text = f"{type(exc).__name__} {str(exc)}".lower()
    transient_markers = (
        "rate limit",
        "rate_limit",
        "overload",
        "overloaded",
        "temporarily unavailable",
        "temporary provider capacity",
        "timeout",
        "timed out",
        "server error",
        "internal server",
        "internal_server_error",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "connection reset",
    )
    return any(marker in safe_text for marker in transient_markers)


def _provider_status_code(exc: Exception) -> Optional[int]:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value

    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int):
        return value
    return None


def _safe_provider_error_type(exc: Exception) -> str:
    status_code = _provider_status_code(exc)
    if status_code is not None:
        return f"{type(exc).__name__}:{status_code}"
    return type(exc).__name__


def _upsert_user(user_info: Dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        existing_user = db.query(User).filter(User.email == user_info["email"]).first()
        if not existing_user:
            db.add(
                User(
                    first_name=user_info["first_name"],
                    last_name=user_info["last_name"],
                    email=user_info["email"],
                    office_name=user_info["office_name"],
                    org_type=user_info["org_type"],
                    phone=user_info.get("phone"),
                )
            )
            db.commit()
            logger.info("User upsert: created for %s", user_info["email"])
            return

        updated = False
        for field in ("first_name", "last_name", "office_name", "org_type", "phone"):
            if field == "phone" and user_info.get(field) is None:
                continue
            if getattr(existing_user, field) != user_info.get(field):
                setattr(existing_user, field, user_info.get(field))
                updated = True
        if updated:
            db.commit()
            logger.info("User upsert: updated for %s", user_info["email"])
        else:
            logger.info("User upsert: existing for %s", user_info["email"])
    except Exception as exc:
        logger.error("Error saving user to database: %s", str(exc))
        db.rollback()
    finally:
        db.close()


def _create_client_submission(user_info: Dict[str, Any], run_id: str) -> str:
    db = SessionLocal()
    try:
        submission = ClientSubmission(
            user_email=user_info["email"],
            first_name=user_info["first_name"],
            last_name=user_info["last_name"],
            office_name=user_info["office_name"],
            org_type=user_info["org_type"],
            phone=user_info.get("phone"),
            ghl_cid=user_info.get("ghl_cid"),
            financial_only_acknowledgement=user_info.get("financial_only_acknowledgement"),
            acknowledgement_timestamp=user_info.get("acknowledgement_timestamp"),
            acknowledgement_ip=user_info.get("acknowledgement_ip"),
            acknowledgement_version=user_info.get("acknowledgement_version"),
            source="client",
            status="submitted",
            analysis_run_id=run_id,
        )
        db.add(submission)
        db.commit()
        db.refresh(submission)
        submission_id = str(submission.id)
        logger.info("[analysis] submission created run_id=%s id=%s", run_id, submission_id)
        return submission_id
    except Exception as exc:
        db.rollback()
        logger.error("[analysis] submission create failed run_id=%s: %s", run_id, str(exc))
        raise PublicAnalyzerError("submission_create_failed", "Unable to create submission.") from exc
    finally:
        db.close()


def _send_public_emails(
    user_info: Dict[str, str],
    file_bytes: bytes,
    file_name: str,
    file_type: str,
    results: Dict[str, Any],
) -> bool:
    email_success = True
    try:
        send_followup_email(user_info, PUBLIC_TOOL_NAME, results)
    except Exception as exc:
        email_success = False
        logger.error(
            "Follow-up email failed for %s (%s): %s",
            user_info["email"],
            file_name,
            str(exc),
        )

    try:
        send_email(user_info, file_bytes, file_name, file_type, results, PUBLIC_TOOL_NAME)
    except Exception as exc:
        email_success = False
        logger.error(
            "Admin email failed for %s (%s): %s",
            user_info["email"],
            file_name,
            str(exc),
        )
    return email_success


def _create_upload_record(
    *,
    user_email: str,
    file_name: str,
    tool_name: str,
    results: Dict[str, Any],
) -> Any:
    db = SessionLocal()
    try:
        analysis_json = json.dumps(
            {
                "raw_analyses": results["raw_analyses"],
                "deduplicated_issues": results["deduplicated_issues"],
                "total_issue_count": results["total_issue_count"],
                "all_trends": results.get("all_trends", []),
            }
        )
        new_upload = Upload(
            file_name=file_name,
            tool_name=tool_name,
            upload_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            user_email=user_email,
            analysis_data=analysis_json,
        )
        db.add(new_upload)
        db.commit()
        db.refresh(new_upload)
        logger.info("Upload committed successfully: %s - %s", file_name, tool_name)
        return new_upload.id
    except Exception as exc:
        db.rollback()
        logger.error("Error saving upload to database for %s: %s", file_name, str(exc))
        raise PublicAnalyzerError("upload_save_failed", "Unable to save upload analysis.") from exc
    finally:
        db.close()


def _mark_submission_completed(submission_id: str, upload_id: Any, cid: Optional[str]) -> None:
    db = SessionLocal()
    try:
        update_submission_status(
            db,
            submission_id,
            status="completed",
            completed_at=datetime.utcnow(),
            error_message=None,
            errored_at=None,
            canceled_at=None,
        )
        db.query(Upload).filter(Upload.id.in_([upload_id])).update(
            {"submission_id": submission_id},
            synchronize_session=False,
        )
        db.commit()
        logger.info("Linked upload %s to submission_id %s", upload_id, submission_id)
    except Exception as exc:
        db.rollback()
        logger.error("Error completing submission %s: %s", submission_id, str(exc))
        raise PublicAnalyzerError("submission_update_failed", "Unable to mark submission completed.") from exc
    finally:
        db.close()

    if cid:
        _handle_ghl_writeback(submission_id, cid)


def _mark_submission_error(
    submission_id: str,
    error_code: str,
    error_message: Optional[str] = None,
) -> None:
    db = SessionLocal()
    try:
        update_submission_status(
            db,
            submission_id,
            status="error",
            errored_at=datetime.utcnow(),
            error_message=error_message or error_code,
        )
    except Exception as exc:
        logger.error("[analysis] submission error update failed: %s", str(exc))
    finally:
        db.close()


def _mark_submission_canceled(
    submission_id: str,
    error_message: Optional[str] = None,
) -> None:
    db = SessionLocal()
    try:
        update_submission_status(
            db,
            submission_id,
            status="canceled",
            canceled_at=datetime.utcnow(),
            completed_at=None,
            errored_at=None,
            error_message=error_message or CANCELED_MESSAGE,
        )
    except Exception as exc:
        logger.error("[analysis] submission cancel update failed: %s", str(exc))
    finally:
        db.close()


_UNSET = object()


def _update_submission_ghl_fields(
    db,
    submission_id: str,
    ghl_cid: Any = _UNSET,
    submitted_at: Any = _UNSET,
    error_msg: Any = _UNSET,
) -> None:
    fields = []
    params = {"id": str(submission_id)}
    if ghl_cid is not _UNSET:
        fields.append("ghl_cid = :ghl_cid")
        params["ghl_cid"] = ghl_cid
    if submitted_at is not _UNSET:
        fields.append("ghl_analyzer_submitted_at = :submitted_at")
        params["submitted_at"] = submitted_at
    if error_msg is not _UNSET:
        fields.append("ghl_analyzer_submitted_error = :error_msg")
        params["error_msg"] = error_msg
    if not fields:
        return
    db.execute(text(f"update client_submissions set {', '.join(fields)} where id = :id"), params)
    db.commit()


def _set_submission_ghl_fields(
    submission_id: str,
    *,
    ghl_cid: Any = _UNSET,
    submitted_at: Any = _UNSET,
    error_msg: Any = _UNSET,
) -> None:
    db = SessionLocal()
    try:
        _update_submission_ghl_fields(
            db,
            submission_id,
            ghl_cid=ghl_cid,
            submitted_at=submitted_at,
            error_msg=error_msg,
        )
    except Exception as exc:
        logger.error("Failed to update GHL fields for submission %s: %s", submission_id, type(exc).__name__)
    finally:
        db.close()


def _handle_ghl_writeback(submission_id: str, cid: str) -> None:
    _set_submission_ghl_fields(submission_id, ghl_cid=cid)

    success, err = _ghl_update_analyzer_submitted(cid)
    if success:
        tag_success, tag_err = _ghl_add_tag(cid, "analyzer submitted")
        if tag_success:
            logger.info("GHL tag added for cid %s", cid)
        else:
            logger.warning("GHL tag add failed for cid %s: %s", cid, tag_err)
        _set_submission_ghl_fields(
            submission_id,
            submitted_at=datetime.utcnow(),
            error_msg=None,
        )
        return

    if err == "missing analyzer field id":
        logger.warning("GHL analyzer field id missing; skipping writeback for cid %s", cid)
    else:
        logger.warning("GHL writeback failed for cid %s: %s", cid, err)
    _set_submission_ghl_fields(submission_id, error_msg=err)


def _ghl_update_analyzer_submitted(cid: str) -> tuple[bool, str]:
    if not cid:
        return False, "missing cid"
    base_url = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com").rstrip("/")
    token = os.getenv("GHL_BEARER_TOKEN", "")
    version = os.getenv("GHL_API_VERSION", "2021-07-28")
    field_id = os.getenv("GHL_ANALYZER_SUBMITTED_FIELD_ID", "").strip()
    if not token:
        return False, "missing bearer token"
    if not field_id:
        return False, "missing analyzer field id"
    url = f"{base_url}/contacts/{cid}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Version": version,
    }
    payload = {"customFields": [{"id": field_id, "value": ["Submitted"]}]}
    try:
        response = requests.put(url, headers=headers, json=payload, timeout=10)
    except requests.RequestException:
        return False, "request failed"
    if response.status_code in (200, 201, 202, 204):
        return True, ""
    if response.status_code in (404, 405):
        alt_url = f"{base_url}/contacts"
        alt_payload = {"id": cid, "customFields": [{"id": field_id, "value": ["Submitted"]}]}
        try:
            alt_response = requests.put(alt_url, headers=headers, json=alt_payload, timeout=10)
        except requests.RequestException:
            return False, "request failed"
        if alt_response.status_code in (200, 201, 202, 204):
            return True, ""
        return False, f"status {alt_response.status_code}"
    return False, f"status {response.status_code}"


def _ghl_add_tag(cid: str, tag_name: str) -> tuple[bool, str]:
    if not cid:
        return False, "missing cid"
    if not tag_name:
        return False, "missing tag name"
    base_url = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com").rstrip("/")
    token = os.getenv("GHL_BEARER_TOKEN", "")
    version = os.getenv("GHL_API_VERSION", "2021-07-28")
    location_id = os.getenv("LOCATION_ID", "").strip()
    if not token:
        return False, "missing bearer token"
    if not location_id:
        logger.warning("[ghl] add_tag missing location id for cid %s", cid)
        return False, "missing location id"
    url = f"{base_url}/contacts/{cid}/tags"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Version": version,
        "LocationId": location_id,
    }
    payload = {"tags": [tag_name]}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
    except requests.RequestException:
        return False, "request failed"
    if response.status_code in (200, 201, 202, 204):
        return True, ""
    body_text = (response.text or "").strip()
    if len(body_text) > 300:
        body_text = body_text[:300]
    logger.warning(
        "[ghl] add_tag failed cid=%s tag=%s status=%s body=%s",
        cid,
        tag_name,
        response.status_code,
        body_text,
    )
    return False, f"status {response.status_code}: {body_text}"
