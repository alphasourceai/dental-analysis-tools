import streamlit as st

import mimetypes
import pandas as pd
import requests
from PIL import Image
import pymupdf as fitz
import pytesseract
import tempfile
import os
import uuid
from io import BytesIO
import hmac
import time
import logging
from sqlalchemy import text
from database import get_db, Base, engine, SessionLocal
from models import get_admin_by_username, Admin, create_admin, User, Upload, ClientSubmission, update_submission_status
from supabase_utils import (
    persist_upload_file,
    update_upload_file_upload_id,
)
from datetime import datetime
from analysis_utils import (
    extract_text_from_pdf,
    openai_analysis,
    xai_analysis,
    anthropic_analysis,
    parse_issues_from_analysis,
    parse_trends_from_analysis,
    deduplicate_issues,
    send_followup_email,
    send_email,
    categorize_issue,
    extract_compelling_insights
)
from admin_dashboard import display_admin_dashboard
from upload_portal import PortalError, complete_upload, create_signed_upload_url, verify_upload_token

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class CancelledError(BaseException):
    pass

def _check_cancel(where: str, run_id: str) -> None:
    if st.session_state.get("cancel_requested"):
        logging.info("[analysis] canceled run_id=%s where=%s", run_id, where)
        raise CancelledError("cancel_requested")

def normalize_email(raw_email: str) -> str:
    if not raw_email:
        return ""
    return raw_email.strip().lower()

def _get_query_params() -> dict:
    if hasattr(st, "query_params"):
        try:
            return dict(st.query_params)
        except Exception:
            return st.experimental_get_query_params()
    return st.experimental_get_query_params()

def _get_single_query_param(params: dict, key: str) -> str:
    if not params:
        return ""
    value = params.get(key)
    if isinstance(value, (list, tuple)):
        return value[0] if value else ""
    return value or ""

@st.cache_data(ttl=300, show_spinner=False)
def _ghl_get_contact(cid: str) -> dict:
    if not cid:
        return {}
    base_url = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com").rstrip("/")
    token = os.getenv("GHL_BEARER_TOKEN", "")
    version = os.getenv("GHL_API_VERSION", "2021-07-28")
    if not base_url or not token:
        return {}
    url = f"{base_url}/contacts/{cid}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Version": version,
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
    except requests.RequestException:
        return {}
    if response.status_code != 200:
        return {}
    try:
        payload = response.json()
    except ValueError:
        return {}
    if isinstance(payload, dict) and isinstance(payload.get("contact"), dict):
        return payload["contact"]
    if isinstance(payload, dict):
        return payload
    return {}

def _extract_office_name(contact: dict) -> str:
    if not contact:
        return ""
    field_id = os.getenv("GHL_OFFICE_FIELD_ID", "").strip()
    if not field_id:
        return ""
    custom_fields = contact.get("customFields") or contact.get("custom_fields") or []
    if not isinstance(custom_fields, list):
        return ""
    for field in custom_fields:
        if str(field.get("id")) == field_id:
            value = field.get("value")
            if isinstance(value, str):
                return value
            if value is None:
                return ""
            return str(value)
    return ""

def _maybe_prefill_from_cid(params: dict) -> None:
    cid = _get_single_query_param(params, "cid")
    if not cid:
        st.session_state["prefill_locked"] = False
        return
    contact = _ghl_get_contact(cid)
    if not contact:
        st.session_state["prefill_locked"] = False
        return
    location_id = os.getenv("LOCATION_ID", "").strip()
    contact_location = contact.get("locationId") or contact.get("location_id")
    if location_id and contact_location and str(contact_location) != location_id:
        st.session_state["prefill_locked"] = False
        return

    first_name = (contact.get("firstName") or contact.get("first_name") or "").strip()
    last_name = (contact.get("lastName") or contact.get("last_name") or "").strip()
    email = (contact.get("email") or "").strip()
    office_name = _extract_office_name(contact).strip()
    if not all([first_name, last_name, email, office_name]):
        st.session_state["prefill_locked"] = False
        return

    st.session_state["contact_first_name"] = first_name
    st.session_state["contact_last_name"] = last_name
    st.session_state["contact_office_name"] = office_name
    st.session_state["contact_email"] = email
    st.session_state["prefill_locked"] = True

_UNSET = object()

def _update_submission_ghl_fields(db, submission_id, ghl_cid=_UNSET, submitted_at=_UNSET, error_msg=_UNSET) -> None:
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
    stmt = text(f"update client_submissions set {', '.join(fields)} where id = :id")
    db.execute(stmt, params)
    db.commit()

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
        logging.warning("[ghl] add_tag missing location id for cid %s", cid)
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
    logging.warning(
        "[ghl] add_tag failed cid=%s tag=%s status=%s body=%s",
        cid,
        tag_name,
        response.status_code,
        body_text,
    )
    return False, f"status {response.status_code}: {body_text}"

def _guess_content_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"

def _portal_max_file_size_mb() -> int:
    try:
        return int(os.getenv("PORTAL_MAX_FILE_SIZE_MB", "50"))
    except ValueError:
        return 50

def _reset_portal_state(raw_token: str) -> None:
    if st.session_state.get("portal_upload_token") == raw_token:
        return
    st.session_state.portal_upload_token = raw_token
    st.session_state.portal_session_token = None
    st.session_state.portal_session_expires_at = None
    st.session_state.portal_request_id = None
    st.session_state.portal_upload_completed = False
    st.session_state.portal_upload_in_progress = False
    if "portal_upload_file" in st.session_state:
        del st.session_state["portal_upload_file"]

def _display_portal_error(exc: PortalError) -> None:
    message = exc.message
    if exc.detail:
        message = f"{message} ({exc.detail})"
    st.error(message)

def _render_upload_portal(raw_token: str) -> None:
    st.markdown("""
        <div class="title-container" style="margin-top: 1.5rem;">
            <h1>Secure Upload Portal</h1>
        </div>
    """, unsafe_allow_html=True)
    st.markdown(
        "<p style='text-align: center; margin-bottom: 1.5rem;'>"
        "Upload your documents securely. This link is single-use and expires shortly."
        "</p>",
        unsafe_allow_html=True,
    )

    if not raw_token:
        st.error("Upload link is missing or invalid. Please request a new link.")
        st.stop()

    _reset_portal_state(raw_token)

    session_token = st.session_state.get("portal_session_token")
    if not session_token:
        try:
            result = verify_upload_token(raw_token)
        except PortalError as exc:
            _display_portal_error(exc)
            st.stop()
        else:
            st.session_state.portal_session_token = result.get("session_token")
            st.session_state.portal_session_expires_at = result.get("session_expires_at")
            st.session_state.portal_request_id = result.get("request_id")
            session_token = st.session_state.portal_session_token

    if st.session_state.get("portal_upload_completed"):
        st.success("Upload complete. You can close this tab.")
        st.stop()

    expires_at = st.session_state.get("portal_session_expires_at")
    if expires_at:
        st.caption(f"Session expires at {expires_at} UTC.")

    st.markdown("### Upload your file")
    st.caption(f"Allowed types: PDF, CSV, TXT, XLS/XLSX. Max size: {_portal_max_file_size_mb()} MB.")

    uploaded_file = st.file_uploader(
        "Choose a file",
        type=["pdf", "csv", "txt", "xls", "xlsx"],
        key="portal_upload_file",
    )

    if not uploaded_file:
        st.info("Select a file to continue.")
        return

    upload_disabled = st.session_state.get("portal_upload_in_progress", False)
    upload_clicked = st.button("Upload File", type="primary", disabled=upload_disabled)

    if upload_clicked:
        st.session_state.portal_upload_in_progress = True
        try:
            raw_content_type = uploaded_file.type or _guess_content_type(uploaded_file.name)
            content_type = (raw_content_type or "").strip().lower()
            if not content_type:
                raise PortalError("invalid_content_type", "Unable to detect file type", status=400)

            file_bytes = uploaded_file.getvalue()
            byte_size = len(file_bytes)
            signed = create_signed_upload_url(
                session_token,
                uploaded_file.name,
                content_type,
                byte_size,
            )
            signed_url = signed.get("signed_url", "")
            upload_id = signed.get("upload_id", "")
            if not signed_url or not upload_id:
                raise PortalError("signer_failed", "Signed upload URL missing", status=502)

            with st.spinner("Uploading to secure storage..."):
                response = requests.put(
                    signed_url,
                    data=file_bytes,
                    headers={"Content-Type": content_type},
                    timeout=60,
                )
            if response.status_code not in (200, 201, 204):
                raise PortalError("upload_failed", "Unable to upload file", status=502)

            complete_upload(session_token, upload_id)
            st.session_state.portal_upload_completed = True
            st.success("Upload complete. You can close this tab.")
            st.stop()
        except PortalError as exc:
            _display_portal_error(exc)
        except requests.RequestException:
            st.error("Unable to upload the file. Please try again.")
        finally:
            st.session_state.portal_upload_in_progress = False

# ---- API Keys ----
# API keys are loaded from environment variables (Replit Secrets)
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SHOW_DEBUG_CID = os.getenv("SHOW_DEBUG_CID", "").strip().lower() in ("1", "true", "yes", "on")
SHOW_DEBUG_ADMIN_ROUTE = os.getenv("SHOW_DEBUG_ADMIN_ROUTE", "").strip().lower() in ("1", "true", "yes", "on")

# ---- Page Config ----
st.set_page_config(page_title="AlphaSource Dental Analysis", page_icon="📊", layout="centered")

# ---- Style ----
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Raleway:wght@400;500;600;700&display=swap');
    
    .stApp {
        background-color: #061551;
        color: #EBFEFF;
        font-family: 'Raleway', system-ui, -apple-system, sans-serif;
    }
    
    /* Remove white bar at top */
    header[data-testid="stHeader"] {
        background-color: #061551 !important;
    }
    
    [data-testid="stAppViewContainer"] {
        background-color: #061551 !important;
    }
    
    [data-testid="stToolbar"] {
        background-color: #061551 !important;
    }
    
    /* Headers and text */
    h1, h2, h3, h4, h5, h6 {
        font-family: 'Raleway', sans-serif;
        color: #EBFEFF;
    }
    
    p, label, .stMarkdown {
        font-family: 'Raleway', sans-serif;
        color: #EBFEFF;
    }
    
    /* Labels */
    label, .stTextInput label, .stSelectbox label, .stFileUploader label {
        color: #EBFEFF !important;
        font-weight: 500;
    }
    
    /* Inputs - light background with black text */
    input, textarea {
        background-color: rgba(255,255,255,0.9) !important;
        color: #000000 !important;
        border: 1px solid rgba(255,255,255,0.14) !important;
        border-radius: 10px !important;
        font-family: 'Raleway', sans-serif;
    }
    
    input::placeholder {
        color: rgba(0,0,0,0.5) !important;
    }
    
    /* Dropdown - light background with black text */
    select, .stSelectbox select {
        background-color: rgba(255,255,255,0.9) !important;
        color: #000000 !important;
        border: 1px solid rgba(255,255,255,0.14) !important;
        border-radius: 10px !important;
        font-family: 'Raleway', sans-serif;
    }
    
    /* Buttons */
    .stButton > button, .stForm button, .stDownloadButton > button {
        background-color: #AD8BF7 !important;
        color: #ffffff !important;
        border: 1px solid #AD8BF7 !important;
        border-radius: 20px !important;
        padding: 8px 14px !important;
        font-weight: 600 !important;
        font-family: 'Raleway', sans-serif;
    }
    
    .stButton > button:hover, .stForm button:hover, .stDownloadButton > button:hover {
        background-color: #854DFF !important;
        border-color: #854DFF !important;
    }
    
    /* Remove glass containers from most elements */
    .stAlert, div[data-testid="stExpander"] {
        background-color: transparent !important;
        border: none !important;
        color: #EBFEFF !important;
    }
    
    /* Info messages - keep subtle background */
    .stInfo {
        background-color: rgba(255,255,255,0.06) !important;
        border: 1px solid rgba(255,255,255,0.14) !important;
        border-radius: 12px !important;
        padding: 1rem !important;
    }
    
    /* Divider */
    hr {
        border-color: rgba(255,255,255,0.14) !important;
    }
    
    /* File uploader styling - keep Streamlit's default clean design */
    .stFileUploader {
        margin-bottom: 0.5rem !important;
    }
    
    /* Hide upload label since we're using custom icons/labels above */
    .stFileUploader label {
        display: none !important;
    }
    
    /* Browse button text color - black */
    .stFileUploader button {
        color: #000000 !important;
    }
    
    /* File uploader placeholder text - keep default black/dark color */
    .stFileUploader [data-testid="stFileUploaderDropzone"] span,
    .stFileUploader [data-testid="stFileUploaderDropzone"] p,
    .stFileUploader [data-testid="stFileUploaderDropzone"] small {
        color: #000000 !important;
    }
    
    /* Uploaded filename text - white color */
    .stFileUploader [data-testid="stFileUploaderFileName"],
    .stFileUploader section[data-testid="stFileUploaderFileData"] span,
    .stFileUploader section[data-testid="stFileUploaderFileData"] small {
        color: #FFFFFF !important;
    }
    
    /* Selectbox dropdown */
    .stSelectbox > div > div {
        background-color: rgba(255,255,255,0.9) !important;
        border-color: rgba(255,255,255,0.14) !important;
    }
    
    /* Success/Info messages */
    .stSuccess {
        background-color: rgba(173,139,247,0.2) !important;
        border-left: 4px solid #AD8BF7 !important;
        color: #EBFEFF !important;
    }
    
    /* Spinner animation */
    .spinner {
        width: 20px;
        height: 20px;
        border: 3px solid rgba(173,139,247,0.3);
        border-top: 3px solid #AD8BF7;
        border-radius: 50%;
        animation: spin 1s linear infinite;
    }
    
    @keyframes spin {
        0% { transform: rotate(0deg); }
        100% { transform: rotate(360deg); }
    }
    
    /* Logo container */
    .logo-container {
        text-align: center;
        padding: 1rem 0;
    }
    
    /* Sidebar - completely hidden */
    [data-testid="stSidebar"] {
        display: none !important;
        visibility: hidden !important;
    }
    
    [data-testid="collapsedControl"] {
        display: none !important;
    }
    
    /* Main content takes full width */
    .main .block-container {
        max-width: 100% !important;
        padding-left: 5rem !important;
        padding-right: 5rem !important;
    }
    
    /* Section headers with icons - vertically centered, contains upload box */
    .section-header {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 1rem;
        padding: 0.75rem 1rem;
        background-color: rgba(255,255,255,0.06);
        border: 1px solid rgba(255,255,255,0.14);
        border-radius: 12px;
        min-height: 60px;
    }
    
    
    .section-icon {
        width: 30px;
        height: 30px;
        stroke: #AD8BF7;
        fill: none;
        flex-shrink: 0;
    }
    
    /* Title containers - vertically centered */
    .title-container {
        background-color: rgba(255,255,255,0.06);
        border: 1px solid rgba(255,255,255,0.14);
        border-radius: 12px;
        padding: 1rem 1.5rem;
        text-align: center;
        margin-bottom: 1rem;
    }
    
    .title-container h1, .title-container h2, .title-container h3 {
        margin: 0;
        line-height: 1.3;
    }
</style>
""", unsafe_allow_html=True)

# ---- Upload Portal Route ----
_query_params = _get_query_params()
_upload_token = _get_single_query_param(_query_params, "upload_token")
_page_param = _get_single_query_param(_query_params, "page").lower()
if SHOW_DEBUG_CID:
    _cid_value = _get_single_query_param(_query_params, "cid")
    _cid_display = _cid_value if _cid_value else "<missing>"
    st.caption(f"Debug: cid={_cid_display}")
if _page_param == "uploads" and not _upload_token:
    _upload_token = _get_single_query_param(_query_params, "token")
if _upload_token or _page_param == "uploads":
    _render_upload_portal(_upload_token)
    st.stop()

# ---- Initialize Session State ----
if 'analysis_complete' not in st.session_state:
    st.session_state.analysis_complete = False
if 'analysis_results' not in st.session_state:
    st.session_state.analysis_results = {}
if 'analyzing' not in st.session_state:
    st.session_state.analyzing = False
if 'is_admin_logged_in' not in st.session_state:
    st.session_state.is_admin_logged_in = False
if 'admin_session' not in st.session_state:
    st.session_state.admin_session = None
if 'admin_user' not in st.session_state:
    st.session_state.admin_user = None
if 'prefill_locked' not in st.session_state:
    st.session_state.prefill_locked = False
if 'cancel_requested' not in st.session_state:
    st.session_state.cancel_requested = False
if 'analysis_run_id' not in st.session_state:
    st.session_state.analysis_run_id = ""
if 'analysis_canceled' not in st.session_state:
    st.session_state.analysis_canceled = False
if 'analysis_submission_id' not in st.session_state:
    st.session_state.analysis_submission_id = ""
if 'pnl_uploader_key_version' not in st.session_state:
    st.session_state.pnl_uploader_key_version = 0

if _page_param in ("admin", "admin_login", "admin_dashboard"):
    st.session_state.page = "Admin Dashboard"
    if SHOW_DEBUG_ADMIN_ROUTE:
        logging.info("admin_route page_param=%s -> Admin Dashboard", _page_param)
elif _page_param in ("analyzer", "home", "public"):
    st.session_state.page = "Analyzer"
    if SHOW_DEBUG_ADMIN_ROUTE:
        logging.info("admin_route page_param=%s -> Analyzer", _page_param)
elif SHOW_DEBUG_ADMIN_ROUTE:
    logging.info("admin_route page_param=%s -> (no override)", _page_param)

# ---- Page Navigation (no sidebar, using session state) ----
if 'page' not in st.session_state:
    st.session_state.page = "Analyzer"

_maybe_prefill_from_cid(_query_params)

# Analyzer Page Content
if st.session_state.page == "Analyzer":
    # Page Title - only on Analyzer page
    st.markdown("""
        <div class="title-container" style="margin-top: 1.5rem;">
            <h1>Dental Operations AI Analysis</h1>
        </div>
    """, unsafe_allow_html=True)
    st.markdown('<div style="height: 1.5rem;"></div>', unsafe_allow_html=True)
    
    # Show results if analysis is complete
    if st.session_state.analysis_complete:
        st.markdown("### Analysis Complete!")
        st.markdown("Thank you for submitting your documents. The analysis results have been sent to your email.")
        st.divider()
        
        # Display consolidated results with deduplicated counts
        total_issues_across_all_docs = 0
        
        for doc_type, results in st.session_state.analysis_results.items():
            st.markdown(f"#### {doc_type}")
            
            # Display issue count
            issue_count = results.get('total_issue_count', 0)
            total_issues_across_all_docs += issue_count
            
            st.markdown(f"**{issue_count} improvement opportunities identified** across 3 AI models")
            
            insights = extract_compelling_insights(results, max_insights=5)
            if insights:
                st.markdown("**Key Insights Identified:**")
                for i, insight in enumerate(insights, 1):
                    st.markdown(f"{i}. {insight}")
                
                deduplicated = results.get('deduplicated_issues', [])
                remaining = len(deduplicated) - len(insights)
                if remaining > 0:
                    st.markdown(f"*...and {remaining} more improvement opportunities*")
            
            st.divider()
        
        # Overall summary
        st.markdown(f"### Total: {total_issues_across_all_docs} improvement opportunities")
        st.markdown("*Detailed analysis has been sent to the consulting team.*")
        
        # Button to start new analysis
        if st.button("Start New Analysis"):
            st.session_state.analysis_complete = False
            st.session_state.analysis_results = {}
            st.session_state.analysis_canceled = False
            st.session_state.cancel_requested = False
            st.session_state.analysis_run_id = ""
            st.session_state.analysis_submission_id = ""
            st.session_state.pnl_uploader_key_version += 1
            st.rerun()
    
    else:
        # Show contact form and upload sections only if analysis is not complete
        if st.session_state.get("analysis_canceled"):
            st.warning("Analysis canceled. No results were saved.")
        # Contact Information Form
        prefill_locked = st.session_state.get("prefill_locked", False)
        with st.form("user_info_form"):
            st.markdown("""
                <div class="section-header">
                    <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
                        <circle cx="12" cy="7" r="4"></circle>
                    </svg>
                    <h3 style="margin: 0;">Contact Information</h3>
                </div>
            """, unsafe_allow_html=True)
            if prefill_locked:
                st.caption("Contact info loaded from your form and locked.")

            def render_required_label(text: str) -> None:
                st.markdown(
                    f'<div style="margin: 0.35rem 0 0.1rem; font-size: 0.95rem;">{text} <span style="opacity: 0.8;">*</span> <span style="font-size: 0.7rem; opacity: 0.6;">required</span></div>',
                    unsafe_allow_html=True,
                )

            render_required_label("First Name")
            first_name = st.text_input(
                "First Name",
                key="contact_first_name",
                disabled=prefill_locked,
                label_visibility="collapsed",
            )
            render_required_label("Last Name")
            last_name = st.text_input(
                "Last Name",
                key="contact_last_name",
                disabled=prefill_locked,
                label_visibility="collapsed",
            )
            render_required_label("Office/Group Name")
            office_name = st.text_input(
                "Office/Group Name",
                key="contact_office_name",
                disabled=prefill_locked,
                label_visibility="collapsed",
            )
            render_required_label("Email Address")
            email = st.text_input(
                "Email Address",
                placeholder="user@example.com",
                key="contact_email",
                disabled=prefill_locked,
                label_visibility="collapsed",
            )
            render_required_label("Type")
            org_type = st.selectbox("Type", ["Location", "Group"], label_visibility="collapsed")
            submit_user_info = st.form_submit_button("Save Info", disabled=prefill_locked)

        import re
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        show_validation = not prefill_locked and (
            submit_user_info or any([first_name, last_name, office_name, email])
        )
        if prefill_locked:
            valid_email = True
        else:
            valid_email = re.match(email_pattern, email) if email else False
        ready_for_analysis = prefill_locked or (
            all([first_name, last_name, office_name, email, org_type]) and valid_email
        )
        
        if show_validation and not first_name:
            st.error("First name is required.")
        if show_validation and not last_name:
            st.error("Last name is required.")
        if show_validation and not office_name:
            st.error("Office/Group name is required.")
        if show_validation:
            if not email:
                st.error("Email address is required.")
            elif not valid_email:
                st.error("Please enter a valid email address (e.g., user@example.com)")
        
        if not ready_for_analysis and not prefill_locked:
            st.info("Please complete the contact information form above before uploading documents.")
        else:
            st.markdown("""
                <div class="title-container" style="margin-top: 2rem;">
                    <h3>Upload Documents for Analysis</h3>
                </div>
            """, unsafe_allow_html=True)
            st.markdown('<div style="height: 1.5rem;"></div>', unsafe_allow_html=True)
            
            # Financial Analysis File Upload Section (formerly P&L)
            st.markdown("""
                <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 0.5rem;">
                    <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <line x1="12" y1="1" x2="12" y2="23"></line>
                        <path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"></path>
                    </svg>
                    <span style="color: #EBFEFF; font-size: 1.31rem; font-weight: 500;">Financial Analysis</span>
                </div>
            """, unsafe_allow_html=True)
            uploader_key = f"pnl_{st.session_state.pnl_uploader_key_version}"
            pnl_file = st.file_uploader(
                "Upload your financial document",
                type=["xlsx", "csv", "pdf"],
                key=uploader_key,
                label_visibility="collapsed",
                disabled=st.session_state.analyzing,
            )
            st.markdown('<div style="height: 1rem;"></div>', unsafe_allow_html=True)

            # SOP File Upload Section - HIDDEN FOR NOW (will be used later with templates)
            # st.markdown("""
            #     <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 0.5rem;">
            #         <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            #             <path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"></path>
            #             <polyline points="14 2 14 8 20 8"></polyline>
            #             <line x1="9" y1="13" x2="15" y2="13"></line>
            #             <line x1="9" y1="17" x2="15" y2="17"></line>
            #         </svg>
            #         <span style="color: #EBFEFF; font-size: 1.31rem; font-weight: 500;">SOP Analysis</span>
            #     </div>
            # """, unsafe_allow_html=True)
            # sop_file = st.file_uploader("Upload your SOP Document", type=["pdf"], key="sop", label_visibility="collapsed")
            sop_file = None  # Hidden for now - will be enabled later with templates

            # Analyze Documents Button
            st.divider()
            uploaded_files = {
                "Financial Analyzer": pnl_file,
            }
            uploaded_count = sum(1 for f in uploaded_files.values() if f is not None)
            
            if uploaded_count > 0:
                st.markdown(f"**Document ready for analysis**")
                
                if 'analyzing' not in st.session_state:
                    st.session_state.analyzing = False
                progress_bar = None
                progress_text = None
                analysis_hint = "Analysis may take a few minutes. Please don't click the button again."
                
                def update_progress(value: int, label: str) -> None:
                    if progress_bar is None or progress_text is None:
                        return
                    progress_bar.progress(value)
                    progress_text.caption(f"{value}% — {label}")
                
                analyze_clicked = st.button(
                    "Analyze Document",
                    type="primary",
                    disabled=st.session_state.analyzing or not ready_for_analysis,
                )
                st.caption(analysis_hint)

                stop_clicked = False
                if st.session_state.analyzing:
                    stop_clicked = st.button("Stop analysis", type="secondary")

                if analyze_clicked:
                    st.session_state.analyzing = True
                    st.session_state.cancel_requested = False
                    st.session_state.analysis_canceled = False
                    st.session_state.analysis_run_id = str(uuid.uuid4())
                    st.session_state.analysis_submission_id = ""
                    st.rerun()

                if stop_clicked:
                    st.session_state.cancel_requested = True
                    st.rerun()

                if st.session_state.analyzing:
                    progress_bar = st.progress(0)
                    progress_text = st.empty()
                    progress_text.caption("0% — Starting")

                    run_id = st.session_state.get("analysis_run_id") or str(uuid.uuid4())
                    st.session_state.analysis_run_id = run_id

                    try:
                        _check_cancel("before_start", run_id)
                        logging.info("[analysis] start run_id=%s", run_id)
                        _check_cancel("before_upload_loop", run_id)
                        update_progress(10, "Upload started")
                        normalized_email = normalize_email(email)
                        logging.info("Normalized email: %s", normalized_email)
                        user_info_dict = {
                            "first_name": first_name,
                            "last_name": last_name,
                            "office_name": office_name,
                            "email": normalized_email,
                            "org_type": org_type,
                        }

                        # Save user to database FIRST, then close the session before AI analysis
                        _check_cancel("before_user_upsert", run_id)
                        db = SessionLocal()
                        try:
                            existing_user = db.query(User).filter(User.email == normalized_email).first()
                            if not existing_user:
                                new_user = User(
                                    first_name=first_name,
                                    last_name=last_name,
                                    email=normalized_email,
                                    office_name=office_name,
                                    org_type=org_type
                                )
                                db.add(new_user)
                                _check_cancel("before_user_upsert_commit", run_id)
                                db.commit()
                                logging.info("User upsert: created for %s", normalized_email)
                            else:
                                updated = False
                                if existing_user.first_name != first_name:
                                    existing_user.first_name = first_name
                                    updated = True
                                if existing_user.last_name != last_name:
                                    existing_user.last_name = last_name
                                    updated = True
                                if existing_user.office_name != office_name:
                                    existing_user.office_name = office_name
                                    updated = True
                                if existing_user.org_type != org_type:
                                    existing_user.org_type = org_type
                                    updated = True
                                if updated:
                                    _check_cancel("before_user_upsert_commit", run_id)
                                    db.commit()
                                    logging.info("User upsert: updated for %s", normalized_email)
                                else:
                                    logging.info("User upsert: existing for %s", normalized_email)
                        except Exception as e:
                            logging.error(f"Error saving user to database: {str(e)}")
                            db.rollback()
                        finally:
                            # Close this session before long-running AI analysis
                            db.close()

                        submission_id = st.session_state.get("analysis_submission_id") or ""
                        if not submission_id:
                            _check_cancel("before_submission_create", run_id)
                            submission_db = SessionLocal()
                            try:
                                submission = ClientSubmission(
                                    user_email=normalized_email,
                                    first_name=first_name,
                                    last_name=last_name,
                                    office_name=office_name,
                                    org_type=org_type,
                                    status="submitted",
                                    analysis_run_id=run_id,
                                )
                                submission_db.add(submission)
                                _check_cancel("before_submission_create_commit", run_id)
                                submission_db.commit()
                                submission_db.refresh(submission)
                                submission_id = str(submission.id)
                                st.session_state.analysis_submission_id = submission_id
                                logging.info(
                                    "[analysis] submission created run_id=%s id=%s",
                                    run_id,
                                    submission_id,
                                )
                            except Exception as exc:
                                logging.error(
                                    "[analysis] submission create failed run_id=%s: %s",
                                    run_id,
                                    str(exc),
                                )
                                submission_db.rollback()
                            finally:
                                submission_db.close()

                        # Initialize debug log in session state
                        if 'debug_log' not in st.session_state:
                            st.session_state.debug_log = []
                        st.session_state.debug_log = []  # Reset for this analysis

                        # Process each uploaded document
                        st.session_state.debug_log.append("🔍 Starting upload processing loop...")
                        upload_ids = []
                        all_emails_sent = True
                        for tool_name, file in uploaded_files.items():
                                if file is not None:
                                    _check_cancel("before_upload_begin", run_id)
                                    st.session_state.debug_log.append(f"🔍 Processing file: {file.name} ({tool_name})")
                                    file.seek(0)
                                    file_content = file.read()
                                    file_name = file.name
                                    file_type = file.type

                                    upload_file_id = persist_upload_file(
                                        file_bytes=file_content,
                                        user_email=normalized_email,
                                        tool_name=tool_name,
                                        original_filename=file_name,
                                        content_type=file_type,
                                    )
                                    update_progress(25, "File stored in Supabase")
                                    _check_cancel("after_upload_complete", run_id)

                                    _check_cancel("before_extraction", run_id)
                                    file.seek(0)

                                    if file.name.endswith(".pdf"):
                                        raw_text = extract_text_from_pdf(file)
                                        data_input = raw_text
                                    else:
                                        df = pd.read_csv(file) if file.name.endswith(".csv") else pd.read_excel(file)
                                        data_input = df.to_string(index=False)
                                    update_progress(45, "Text extraction complete")

                                    st.session_state.debug_log.append(f"🔍 Running AI analysis for {file.name}...")
                                    update_progress(70, "AI analysis running")
                                    _check_cancel("before_openai", run_id)
                                    openai_result = openai_analysis(data_input)
                                    _check_cancel("after_openai", run_id)
                                    _check_cancel("before_xai", run_id)
                                    xai_result = xai_analysis(data_input)
                                    _check_cancel("after_xai", run_id)
                                    _check_cancel("before_anthropic", run_id)
                                    anthropic_result = anthropic_analysis(data_input)
                                    _check_cancel("after_anthropic", run_id)

                                    openai_issues = parse_issues_from_analysis(openai_result, "OpenAI GPT-4")
                                    xai_issues = parse_issues_from_analysis(xai_result, "xAI Grok")
                                    anthropic_issues = parse_issues_from_analysis(anthropic_result, "Anthropic Claude")

                                    openai_trends = parse_trends_from_analysis(openai_result, "OpenAI GPT-4")
                                    xai_trends = parse_trends_from_analysis(xai_result, "xAI Grok")
                                    anthropic_trends = parse_trends_from_analysis(anthropic_result, "Anthropic Claude")

                                    all_issues = openai_issues + xai_issues + anthropic_issues
                                    all_trends = openai_trends + xai_trends + anthropic_trends

                                    deduplicated_issues = deduplicate_issues(all_issues)

                                    results = {
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
                                        "total_issue_count": len(deduplicated_issues)
                                    }

                                    _check_cancel("before_results_assignment", run_id)
                                    st.session_state.debug_log.append(f"✅ Analysis complete for {file.name}")
                                    st.session_state.analysis_results[tool_name] = results

                                    _check_cancel("before_email_send", run_id)
                                    st.session_state.debug_log.append(f"📧 Sending emails for {file.name}...")
                                    update_progress(90, "Emails sending")
                                    email_success = True
                                    try:
                                        send_followup_email(user_info_dict, tool_name, results)
                                    except Exception as exc:
                                        email_success = False
                                        logging.error(
                                            "Follow-up email failed for %s (%s): %s",
                                            normalized_email,
                                            file_name,
                                            str(exc),
                                        )
                                    try:
                                        send_email(user_info_dict, file_content, file_name, file_type, results, tool_name)
                                    except Exception as exc:
                                        email_success = False
                                        logging.error(
                                            "Admin email failed for %s (%s): %s",
                                            normalized_email,
                                            file_name,
                                            str(exc),
                                        )
                                    if email_success:
                                        st.session_state.debug_log.append(f"✅ Emails sent for {file.name}")
                                    else:
                                        all_emails_sent = False
                                        st.session_state.debug_log.append(f"❌ Email send failed for {file.name}")

                                    _check_cancel("before_upload_save", run_id)
                                    st.session_state.debug_log.append(f"💾 Opening new database session for {file.name}...")
                                    upload_db = SessionLocal()
                                    try:
                                        import json
                                        logging.info(f"Starting database save for {file.name}")

                                        st.session_state.debug_log.append(f"🔍 Serializing analysis to JSON...")
                                        analysis_json = json.dumps({
                                            'raw_analyses': results['raw_analyses'],
                                            'deduplicated_issues': results['deduplicated_issues'],
                                            'total_issue_count': results['total_issue_count'],
                                            'all_trends': results.get('all_trends', [])
                                        })
                                        st.session_state.debug_log.append(f"✅ JSON serialized, length: {len(analysis_json)}")
                                        logging.info(f"Analysis JSON serialized, length: {len(analysis_json)}")

                                        st.session_state.debug_log.append(f"🔍 Creating Upload object...")
                                        new_upload = Upload(
                                            file_name=file.name,
                                            tool_name=tool_name,
                                            upload_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                            user_email=normalized_email,
                                            analysis_data=analysis_json
                                        )
                                        st.session_state.debug_log.append(f"✅ Upload object created")
                                        logging.info(f"Upload object created for {file.name}")

                                        st.session_state.debug_log.append(f"🔍 Adding upload to database session...")
                                        upload_db.add(new_upload)
                                        st.session_state.debug_log.append(f"✅ Upload added to session")
                                        logging.info(f"Upload added to session for {file.name}")

                                        st.session_state.debug_log.append(f"🔍 Committing to database...")
                                        _check_cancel("before_upload_commit", run_id)
                                        upload_db.commit()
                                        st.session_state.debug_log.append(f"✅ Database commit successful!")
                                        logging.info(f"✅ Upload committed successfully: {file.name} - {tool_name}")

                                        _check_cancel("before_upload_file_link", run_id)
                                        update_upload_file_upload_id(upload_file_id, new_upload.id)
                                        upload_ids.append(new_upload.id)

                                        st.session_state.debug_log.append(f"✅ Upload saved to database: {file.name}")
                                    except json.JSONDecodeError as e:
                                        st.session_state.debug_log.append(f"❌ JSON error: {str(e)}")
                                        logging.error(f"❌ JSON serialization error for {file.name}: {str(e)}")
                                        upload_db.rollback()
                                    except Exception as e:
                                        st.session_state.debug_log.append(f"❌ Database error: {type(e).__name__}: {str(e)}")
                                        logging.error(f"❌ Error saving upload to database for {file.name}: {str(e)}")
                                        logging.error(f"Exception type: {type(e).__name__}")
                                        logging.error(f"Results keys: {results.keys() if results else 'None'}")
                                        import traceback
                                        logging.error(f"Traceback: {traceback.format_exc()}")
                                        upload_db.rollback()
                                    finally:
                                        upload_db.close()

                        _check_cancel("before_submission_save", run_id)
                        submission_id = submission_id or st.session_state.get("analysis_submission_id") or ""
                        if upload_ids and all_emails_sent:
                            submission_db = SessionLocal()
                            try:
                                if not submission_id:
                                    submission = ClientSubmission(
                                        user_email=normalized_email,
                                        first_name=first_name,
                                        last_name=last_name,
                                        office_name=office_name,
                                        org_type=org_type,
                                        status="completed",
                                        completed_at=datetime.utcnow(),
                                        analysis_run_id=run_id,
                                    )
                                    submission_db.add(submission)
                                    _check_cancel("before_submission_commit", run_id)
                                    submission_db.commit()
                                    submission_db.refresh(submission)
                                    submission_id = str(submission.id)
                                    st.session_state.analysis_submission_id = submission_id
                                    logging.info(
                                        "Submission snapshot created: %s for %s",
                                        submission.id,
                                        normalized_email,
                                    )
                                else:
                                    _check_cancel("before_submission_status_complete", run_id)
                                    update_submission_status(
                                        submission_db,
                                        submission_id,
                                        status="completed",
                                        completed_at=datetime.utcnow(),
                                        error_message=None,
                                        errored_at=None,
                                        canceled_at=None,
                                    )

                                ghl_cid = _get_single_query_param(_query_params, "cid")
                                if ghl_cid:
                                    _check_cancel("before_submission_ghl_cid", run_id)
                                    try:
                                        _update_submission_ghl_fields(submission_db, submission_id, ghl_cid=ghl_cid)
                                    except Exception as exc:
                                        logging.error(
                                            "Failed to set GHL cid for submission %s: %s",
                                            submission_id,
                                            type(exc).__name__,
                                        )

                                    _check_cancel("before_ghl_writeback", run_id)
                                    success, err = _ghl_update_analyzer_submitted(ghl_cid)
                                    _check_cancel("after_ghl_writeback", run_id)
                                    if success:
                                        _check_cancel("before_ghl_tag", run_id)
                                        tag_success, tag_err = _ghl_add_tag(ghl_cid, "analyzer submitted")
                                        if tag_success:
                                            logging.info("GHL tag added for cid %s", ghl_cid)
                                        else:
                                            logging.warning("GHL tag add failed for cid %s: %s", ghl_cid, tag_err)
                                        try:
                                            _check_cancel("before_submission_ghl_success_update", run_id)
                                            _update_submission_ghl_fields(
                                                submission_db,
                                                submission_id,
                                                submitted_at=datetime.utcnow(),
                                                error_msg=None,
                                            )
                                        except Exception as exc:
                                            logging.error(
                                                "Failed to update GHL writeback status for submission %s: %s",
                                                submission_id,
                                                type(exc).__name__,
                                            )
                                    else:
                                        if err == "missing analyzer field id":
                                            logging.warning(
                                                "GHL analyzer field id missing; skipping writeback for cid %s",
                                                ghl_cid,
                                            )
                                        else:
                                            logging.warning(
                                                "GHL writeback failed for cid %s: %s",
                                                ghl_cid,
                                                err,
                                            )
                                        try:
                                            _check_cancel("before_submission_ghl_error_update", run_id)
                                            _update_submission_ghl_fields(
                                                submission_db,
                                                submission_id,
                                                error_msg=err,
                                            )
                                        except Exception as exc:
                                            logging.error(
                                                "Failed to record GHL writeback error for submission %s: %s",
                                                submission_id,
                                                type(exc).__name__,
                                            )

                                _check_cancel("before_submission_link_uploads", run_id)
                                submission_db.query(Upload).filter(Upload.id.in_(upload_ids)).update(
                                    {"submission_id": submission_id},
                                    synchronize_session=False
                                )
                                _check_cancel("before_submission_link_commit", run_id)
                                submission_db.commit()
                                logging.info(
                                    "Linked %d uploads to submission_id %s",
                                    len(upload_ids),
                                    submission_id,
                                )
                            except Exception as e:
                                logging.error(
                                    "Error creating submission snapshot for %s: %s",
                                    normalized_email,
                                    str(e),
                                )
                                submission_db.rollback()
                            finally:
                                submission_db.close()
                        elif upload_ids and not all_emails_sent:
                            logging.warning(
                                "Submission snapshot skipped for %s due to email failure",
                                normalized_email,
                            )
                            if submission_id:
                                submission_db = SessionLocal()
                                try:
                                    update_submission_status(
                                        submission_db,
                                        submission_id,
                                        status="error",
                                        errored_at=datetime.utcnow(),
                                        error_message="email_failed",
                                    )
                                except Exception as exc:
                                    logging.error(
                                        "[analysis] submission error update failed run_id=%s: %s",
                                        run_id,
                                        str(exc),
                                    )
                                finally:
                                    submission_db.close()

                        update_progress(100, "Analysis complete")
                        # Reset analyzing state and mark analysis as complete
                        st.session_state.analyzing = False
                        st.session_state.analysis_complete = True
                        st.session_state.analysis_canceled = False
                        st.session_state.cancel_requested = False
                        logging.info("[analysis] finished run_id=%s", run_id)
                        st.rerun()
                    except CancelledError:
                        submission_id = st.session_state.get("analysis_submission_id") or ""
                        if submission_id:
                            cancel_db = SessionLocal()
                            try:
                                update_submission_status(
                                    cancel_db,
                                    submission_id,
                                    status="canceled",
                                    canceled_at=datetime.utcnow(),
                                )
                            except Exception as exc:
                                logging.error(
                                    "[analysis] cancel status update failed run_id=%s: %s",
                                    run_id,
                                    str(exc),
                                )
                            finally:
                                cancel_db.close()
                        st.session_state.analyzing = False
                        st.session_state.analysis_complete = False
                        st.session_state.analysis_results = {}
                        st.session_state.cancel_requested = False
                        st.session_state.analysis_canceled = True
                        st.session_state.analysis_run_id = ""
                        st.session_state.analysis_submission_id = ""
                        st.session_state.pnl_uploader_key_version += 1
                        logging.info("[analysis] canceled run_id=%s", run_id)
                        st.rerun()
                    except Exception as exc:
                        submission_id = st.session_state.get("analysis_submission_id") or ""
                        if submission_id:
                            error_db = SessionLocal()
                            try:
                                update_submission_status(
                                    error_db,
                                    submission_id,
                                    status="error",
                                    errored_at=datetime.utcnow(),
                                    error_message=str(exc)[:300],
                                )
                            except Exception as update_exc:
                                logging.error(
                                    "[analysis] error status update failed run_id=%s: %s",
                                    run_id,
                                    str(update_exc),
                                )
                            finally:
                                error_db.close()
                        st.session_state.analyzing = False
                        st.session_state.analysis_complete = False
                        st.session_state.analysis_results = {}
                        st.session_state.cancel_requested = False
                        st.session_state.analysis_canceled = False
                        st.session_state.analysis_run_id = ""
                        st.session_state.analysis_submission_id = ""
                        st.session_state.pnl_uploader_key_version += 1
                        logging.error("[analysis] error run_id=%s: %s", run_id, str(exc))
                        st.rerun()

# Admin Setup Page (for initial production setup)
elif st.session_state.page == "Admin Setup":
    st.markdown("""
        <div class="title-container" style="margin-top: 1.5rem;">
            <h1>Admin Setup</h1>
        </div>
    """, unsafe_allow_html=True)
    st.info("Admin setup is now managed in Supabase Auth. Ask Jason to add your auth user_id to admin_users.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Back to Analyzer", key="setup_to_analyzer_deprecated", use_container_width=True):
            st.session_state.page = "Analyzer"
            st.rerun()
    with col2:
        if st.button("Go to Admin Login", key="setup_to_login_deprecated", use_container_width=True):
            st.session_state.page = "Admin Dashboard"
            st.rerun()
    st.stop()

    # Initialize session state for attempt tracking
    if 'setup_failed_attempts' not in st.session_state:
        st.session_state.setup_failed_attempts = 0
    if 'setup_next_allowed_time' not in st.session_state:
        st.session_state.setup_next_allowed_time = 0
    
    # Server-side check: only allow if no admins exist
    try:
        db = next(get_db())
        try:
            admin_count = db.query(Admin).count()
            if admin_count > 0:
                logging.info("Admin Setup access blocked: admins already exist")
                st.error("Admin Setup is disabled. Admin accounts already exist.")
                st.info("Please use the Admin Dashboard login page to access the dashboard.")
                col1, col2, col3 = st.columns([1, 1, 1])
                with col2:
                    if st.button("Go to Admin Login", key="setup_disabled_to_login", use_container_width=True):
                        st.session_state.page = "Admin Dashboard"
                        st.rerun()
                st.stop()
        finally:
            db.close()
    except StopIteration:
        logging.error("Admin Setup access failed: database connection error")
        st.error("Database connection error. Please contact the administrator.")
        st.stop()
    except Exception as e:
        logging.error(f"Admin Setup access failed: {str(e)}")
        st.error(f"Error checking database: {str(e)}")
        st.stop()
    
    st.markdown("""
        <div class="title-container" style="margin-top: 1.5rem;">
            <h1>Admin Setup</h1>
        </div>
    """, unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; margin-bottom: 2rem; margin-top: 1.5rem;'>Create admin accounts for the dashboard. This page is for initial production setup.</p>", unsafe_allow_html=True)
    
    # Check if locked out due to too many failed attempts
    current_time = time.time()
    if current_time < st.session_state.setup_next_allowed_time:
        wait_seconds = int(st.session_state.setup_next_allowed_time - current_time)
        st.error(f"Setup temporarily locked due to multiple failed attempts. Please try again in {wait_seconds} seconds.")
        logging.warning(f"Admin Setup locked: {wait_seconds}s remaining")
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Back to Analyzer", key="setup_locked_to_analyzer", use_container_width=True):
                st.session_state.page = "Analyzer"
                st.rerun()
        with col2:
            if st.button("Go to Admin Login", key="setup_locked_to_login", use_container_width=True):
                st.session_state.page = "Admin Dashboard"
                st.rerun()
        st.stop()
    
    # Hard lockout after 5 failed attempts
    if st.session_state.setup_failed_attempts >= 5:
        st.error("Setup has been permanently locked due to multiple failed attempts. Please contact the system administrator.")
        logging.error("Admin Setup permanently locked: exceeded 5 failed attempts")
        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            if st.button("Back to Analyzer", key="setup_perm_locked", use_container_width=True):
                st.session_state.page = "Analyzer"
                st.rerun()
        st.stop()
    
    # Setup form with token verification
    with st.form("admin_setup_form"):
        st.markdown("""
            <div class="section-header">
                <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path>
                    <circle cx="8.5" cy="7" r="4"></circle>
                    <path d="M20 8v6M23 11h-6"></path>
                </svg>
                <span class="section-title">Create Admin Account</span>
            </div>
        """, unsafe_allow_html=True)
        
        setup_token = st.text_input("Setup Token (required for first-time setup)", type="password", key="setup_token", help="Contact the system administrator for the setup token")
        setup_username = st.text_input("Username", key="setup_username")
        setup_password = st.text_input("Password", type="password", key="setup_password")
        setup_password_confirm = st.text_input("Confirm Password", type="password", key="setup_password_confirm")
        
        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            create_button = st.form_submit_button("Create Admin", use_container_width=True)
        
        if create_button:
            # Verify setup token first (using constant-time comparison to prevent timing attacks)
            admin_setup_token = os.getenv("ADMIN_SETUP_TOKEN", "")
            
            # Validate environment configuration
            if not admin_setup_token:
                logging.error("Admin Setup attempt failed: ADMIN_SETUP_TOKEN not configured")
                st.error("Admin Setup is not configured properly. Please contact the system administrator.")
                st.stop()
            elif len(admin_setup_token) < 32:
                logging.error(f"Admin Setup attempt failed: ADMIN_SETUP_TOKEN too short (length: {len(admin_setup_token)})")
                st.error("Admin Setup is not configured properly. Please contact the system administrator.")
                st.stop()
            
            # Check for authentication failure (generic error message to prevent information leakage)
            auth_failed = False
            failure_reason = ""
            
            if not setup_token:
                auth_failed = True
                failure_reason = "missing token"
            elif not hmac.compare_digest(setup_token, admin_setup_token):
                auth_failed = True
                failure_reason = "invalid token"
            elif not setup_username or not setup_password:
                auth_failed = True
                failure_reason = "missing credentials"
            elif setup_password != setup_password_confirm:
                auth_failed = True
                failure_reason = "password mismatch"
            
            if auth_failed:
                # Increment failed attempts and apply exponential backoff
                st.session_state.setup_failed_attempts += 1
                attempts = st.session_state.setup_failed_attempts
                
                # Calculate backoff delay: 2^attempts seconds (capped at 300s / 5 minutes)
                backoff_delay = min(2 ** attempts, 300)
                st.session_state.setup_next_allowed_time = time.time() + backoff_delay
                
                # Log the failure (without exposing sensitive details)
                logging.warning(f"Admin Setup authentication failed (attempt {attempts}): {failure_reason}, backoff {backoff_delay}s")
                
                # Generic error message (no specific hints)
                st.error("Authentication failed. Please check your credentials and try again.")
                
                # Clear sensitive form data
                if "setup_token" in st.session_state:
                    del st.session_state["setup_token"]
                if "setup_password" in st.session_state:
                    del st.session_state["setup_password"]
                if "setup_password_confirm" in st.session_state:
                    del st.session_state["setup_password_confirm"]
                    
                st.stop()
            
            # If we reach here, authentication succeeded
            else:
                try:
                    # Create tables if they don't exist
                    Base.metadata.create_all(bind=engine)
                    
                    db = next(get_db())
                    try:
                        # Double-check no admins exist (prevent race condition)
                        admin_count = db.query(Admin).count()
                        if admin_count > 0:
                            st.error("Admin accounts already exist. Setup is now disabled.")
                        else:
                            # Check if this specific admin already exists
                            existing = db.query(Admin).filter(Admin.username == setup_username).first()
                            if existing:
                                st.error(f"Admin '{setup_username}' already exists.")
                            else:
                                # Create admin
                                create_admin(db, setup_username, setup_password)
                                logging.info(f"Admin account created successfully: username='{setup_username}'")
                                st.success(f"✓ Admin account '{setup_username}' created successfully!")
                                st.info("You can now log in to the Admin Dashboard.")
                                
                                # Reset failed attempts counter on successful creation
                                st.session_state.setup_failed_attempts = 0
                                st.session_state.setup_next_allowed_time = 0
                                
                                # Clear form values
                                if "setup_token" in st.session_state:
                                    del st.session_state["setup_token"]
                                if "setup_username" in st.session_state:
                                    del st.session_state["setup_username"]
                                if "setup_password" in st.session_state:
                                    del st.session_state["setup_password"]
                                if "setup_password_confirm" in st.session_state:
                                    del st.session_state["setup_password_confirm"]
                    finally:
                        db.close()
                except StopIteration:
                    st.error("Database connection error. Please try again later.")
                except Exception as e:
                    st.error(f"Error creating admin: {str(e)}")
    
    # Show existing admins
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("### Existing Admins")
    try:
        db = next(get_db())
        try:
            admins = db.query(Admin).all()
            if admins:
                for admin in admins:
                    st.markdown(f"- {admin.username}")
            else:
                st.info("No admin accounts exist yet.")
        finally:
            db.close()
    except Exception as e:
        st.warning(f"Could not retrieve admin list: {str(e)}")
    
    # Navigation buttons
    st.markdown("<br>", unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Back to Analyzer", key="setup_to_analyzer", use_container_width=True):
            st.session_state.page = "Analyzer"
            st.rerun()
    with col2:
        if st.button("Go to Admin Login", key="setup_to_login", use_container_width=True):
            st.session_state.page = "Admin Dashboard"
            st.rerun()

# Admin Dashboard Content
elif st.session_state.page == "Admin Dashboard":
    display_admin_dashboard()

# ---- Footer ----
st.markdown("""<hr style="margin-top: 3rem;">""", unsafe_allow_html=True)

st.markdown("<p style='text-align: center; margin-top: 1rem;'>Built by <a href='https://alphasourceai.com' target='_blank'>AlphaSource AI</a></p>", unsafe_allow_html=True)
