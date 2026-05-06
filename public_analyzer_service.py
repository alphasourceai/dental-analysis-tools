from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import uuid
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, Optional

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


class PublicAnalyzerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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
) -> Dict[str, Any]:
    """Run the existing public analyzer workflow without Streamlit session state."""
    del source_path  # The current schema does not store the originating frontend path.

    submission_id: Optional[str] = None
    upload_id: Optional[str] = None
    run_id = str(uuid.uuid4())

    try:
        validated = _validate_submission_input(
            first_name=first_name,
            last_name=last_name,
            office_name=office_name,
            email=email,
            org_type=org_type,
            uploaded_file_bytes=uploaded_file_bytes,
            original_filename=original_filename,
        )
        normalized_email = validated["email"]
        file_name = validated["original_filename"]
        file_type = (content_type or _guess_content_type(file_name)).strip()
        file_bytes = bytes(uploaded_file_bytes)

        user_info = {
            "first_name": first_name,
            "last_name": last_name,
            "office_name": office_name,
            "email": normalized_email,
            "org_type": org_type,
        }

        logger.info("[analysis] start run_id=%s source=public_service", run_id)
        log_active_models(run_id)

        _upsert_user(user_info)
        submission_id = _create_client_submission(user_info, run_id)

        upload_file_id = persist_upload_file(
            file_bytes=file_bytes,
            user_email=normalized_email,
            tool_name=PUBLIC_TOOL_NAME,
            original_filename=file_name,
            content_type=file_type,
        )
        if not upload_file_id:
            raise PublicAnalyzerError("storage_failed", "Unable to persist uploaded file.")

        data_input = _extract_data_input(file_bytes, file_name)
        results = _run_public_analysis(data_input)
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
) -> Dict[str, str]:
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

    return {
        "email": normalized_email,
        "original_filename": filename,
    }


def normalize_email(raw_email: str) -> str:
    if not raw_email:
        return ""
    return raw_email.strip().lower()


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


def _run_public_analysis(data_input: str) -> Dict[str, Any]:
    openai_result = openai_analysis(data_input)
    xai_result = xai_analysis(data_input)
    anthropic_result = anthropic_analysis(data_input)

    model_labels = get_model_labels()
    openai_issues = parse_issues_from_analysis(openai_result, model_labels["openai"])
    xai_issues = parse_issues_from_analysis(xai_result, model_labels["xai"])
    anthropic_issues = parse_issues_from_analysis(anthropic_result, model_labels["anthropic"])

    openai_trends = parse_trends_from_analysis(openai_result, model_labels["openai"])
    xai_trends = parse_trends_from_analysis(xai_result, model_labels["xai"])
    anthropic_trends = parse_trends_from_analysis(anthropic_result, model_labels["anthropic"])

    all_issues = openai_issues + xai_issues + anthropic_issues
    all_trends = openai_trends + xai_trends + anthropic_trends
    deduplicated_issues = deduplicate_issues(all_issues)

    return {
        "raw_analyses": {
            "OpenAI Analysis": openai_result,
            "xAI Analysis": xai_result,
            "AnthropicAI Analysis": anthropic_result,
        },
        "parsed_issues": {
            "openai": openai_issues,
            "xai": xai_issues,
            "anthropic": anthropic_issues,
        },
        "parsed_trends": {
            "openai": openai_trends,
            "xai": xai_trends,
            "anthropic": anthropic_trends,
        },
        "all_trends": all_trends,
        "deduplicated_issues": deduplicated_issues,
        "total_issue_count": len(deduplicated_issues),
    }


def _upsert_user(user_info: Dict[str, str]) -> None:
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
                )
            )
            db.commit()
            logger.info("User upsert: created for %s", user_info["email"])
            return

        updated = False
        for field in ("first_name", "last_name", "office_name", "org_type"):
            if getattr(existing_user, field) != user_info[field]:
                setattr(existing_user, field, user_info[field])
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


def _create_client_submission(user_info: Dict[str, str], run_id: str) -> str:
    db = SessionLocal()
    try:
        submission = ClientSubmission(
            user_email=user_info["email"],
            first_name=user_info["first_name"],
            last_name=user_info["last_name"],
            office_name=user_info["office_name"],
            org_type=user_info["org_type"],
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
