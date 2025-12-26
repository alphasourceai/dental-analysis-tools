import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import streamlit as st
from sqlalchemy import func

from database import SessionLocal
from models import ClientSubmission, Upload, UploadPortalFile, User, delete_user, get_db
from supabase_utils import persist_upload_file, update_upload_file_upload_id
from upload_portal import PortalError, create_upload_request

def normalize_email(raw_email: str) -> str:
    if not raw_email:
        return ""
    return raw_email.strip().lower()


def _token_ttl_minutes() -> int:
    raw = os.getenv("PORTAL_TOKEN_TTL_MINUTES", "60")
    try:
        return max(1, int(raw))
    except ValueError:
        return 60


def _parse_date_input(value: str) -> datetime.date:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def format_mst(dt: datetime) -> str:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if ZoneInfo:
        mst = dt.astimezone(ZoneInfo("America/Denver"))
        return mst.strftime("%m-%d-%Y %H:%M MST")
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%m-%d-%Y %H:%M UTC")


def _format_admin_dt(value: object):
    if not value:
        return None
    dt = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            try:
                dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return raw
    else:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if ZoneInfo:
        mst = dt.astimezone(ZoneInfo("America/Denver"))
        return mst.strftime("%m-%d-%Y %H:%M MST")
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%m-%d-%Y %H:%M UTC")


def _parse_analysis_json(value: object):
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
            return parsed if isinstance(parsed, dict) else None
        except (TypeError, json.JSONDecodeError):
            return None
    return None

# Admin Dashboard Page
def display_admin_dashboard():
    st.markdown("""
    <style>
    details > summary {
        background-color: #061551 !important;
        color: #EBFEFF !important;
    }
    details > summary:hover,
    details > summary:focus,
    details > summary:active,
    details > summary:focus-visible {
        background-color: #061551 !important;
        color: #EBFEFF !important;
    }
    [data-baseweb="tab-panel"]:first-of-type [data-testid="column"] p {
        font-size: 0.75rem !important;
        line-height: 1.2 !important;
        margin: 0.15rem 0 !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
    }
    [data-baseweb="tab-panel"]:first-of-type [data-testid="column"] .stMarkdown strong {
        font-size: 0.8rem !important;
        font-weight: 600 !important;
    }
    [data-baseweb="tab-panel"]:first-of-type .stButton > button {
        padding: 0.15rem 0.3rem !important;
        font-size: 1rem !important;
        min-height: 1.6rem !important;
        height: 1.6rem !important;
        line-height: 1 !important;
        border-radius: 4px !important;
    }
    [data-baseweb="tab-panel"]:first-of-type [data-testid="column"] {
        padding: 0.1rem 0.3rem !important;
    }
    [data-baseweb="tab-panel"]:first-of-type [data-testid="stTextArea"] p,
    [data-baseweb="tab-panel"]:first-of-type .stAlert p,
    [data-baseweb="tab-panel"]:first-of-type .stWarning p,
    [data-baseweb="tab-panel"]:first-of-type .stError p,
    [data-baseweb="tab-panel"]:first-of-type .stSuccess p,
    [data-baseweb="tab-panel"]:first-of-type .stInfo p {
        font-size: 1rem !important;
        white-space: normal !important;
    }
    .as-card {
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 14px;
        padding: 0.75rem 0.9rem;
        margin-bottom: 0.85rem;
    }
    .as-subcard {
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 12px;
        padding: 0.6rem 0.75rem;
        margin: 0.6rem 0;
    }
    .as-muted {
        color: #A9B2C9;
        font-size: 0.75rem;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        margin-bottom: 0.15rem;
    }
    .as-pill {
        display: inline-block;
        padding: 0.1rem 0.5rem;
        border-radius: 999px;
        background: rgba(0, 207, 200, 0.2);
        border: 1px solid rgba(0, 207, 200, 0.35);
        color: #E6FBFF;
        font-size: 0.75rem;
        font-weight: 600;
    }
    .as-card a,
    .as-card a:visited,
    .as-subcard a,
    .as-subcard a:visited {
        color: #E6EBFF !important;
        text-decoration: none !important;
    }
    .as-card a:hover,
    .as-subcard a:hover {
        color: #FFFFFF !important;
        text-decoration: underline !important;
    }

    [data-baseweb="tab-panel"]:first-of-type a,
    [data-baseweb="tab-panel"]:first-of-type a:visited,
    [data-baseweb="tab-panel"]:first-of-type a:active {
        color: #E6EBFF !important;
        text-decoration: none !important;
    }
    [data-baseweb="tab-panel"]:first-of-type a:hover {
        color: #FFFFFF !important;
        text-decoration: underline !important;
    }

    .as-card details {
        background: rgba(6, 21, 81, 0.35);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 10px;
        padding: 0.35rem 0.6rem;
        margin-top: 0.5rem;
    }
    .as-subcard details {
        background: rgba(255, 255, 255, 0.02);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 10px;
        padding: 0.35rem 0.6rem;
        margin-top: 0.55rem;
    }
    </style>
    """, unsafe_allow_html=True)

    # Header with logout button
    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown("<h1 style='margin-top: 1.5rem;'>Admin Dashboard</h1>", unsafe_allow_html=True)
    with col2:
        if st.button("Logout", key="logout_button", type="secondary"):
            st.session_state.is_admin_logged_in = False
            st.session_state.admin_session = None
            st.session_state.admin_user = None
            st.session_state.page = "Analyzer"
            st.rerun()
    
    # Tab navigation
    tab1, tab2, tab3, tab4 = st.tabs(
        ["Client Submissions", "Document Analysis", "Admin Management", "Secure Uploads"]
    )
    
    with tab1:
        display_client_submissions()
    
    with tab2:
        display_document_analysis()
    
    with tab3:
        display_admin_management()

    with tab4:
        display_upload_requests()


def display_upload_requests():
    st.markdown("<h3 style='margin-top: 1.5rem;'>Secure Upload Requests</h3>", unsafe_allow_html=True)
    st.markdown(
        "<p>Send a single-use magic link for clients to upload PHI securely.</p>",
        unsafe_allow_html=True,
    )

    with st.form("upload_request_form"):
        client_email = st.text_input("Client email")
        submit_request = st.form_submit_button("Send Magic Link")

    if submit_request:
        try:
            result = create_upload_request(client_email)
            st.success("Magic link sent successfully.")
            st.markdown("**Upload Request Details**")
            st.write(f"Request ID: {result.get('request_id')}")
            st.write(f"Expires in {_token_ttl_minutes()} minutes")
        except PortalError as exc:
            st.error(f"Unable to create upload request: {exc.message}")
        except Exception as exc:
            st.error(f"Unexpected error: {exc}")

    st.divider()
    display_uploads_inbox()


def display_uploads_inbox():
    st.markdown("<h3 style='margin-top: 1.5rem;'>Uploads Inbox</h3>", unsafe_allow_html=True)
    st.markdown(
        "<p>Most recent upload portal files (default 50). Use filters to narrow results.</p>",
        unsafe_allow_html=True,
    )

    completed_only = st.checkbox("Completed only", value=True, key="uploads_inbox_completed_only")
    user_email_filter = st.text_input(
        "Filter by user email (contains)",
        placeholder="name@example.com",
        key="uploads_inbox_email_filter",
    ).strip()

    date_cols = st.columns(2)
    with date_cols[0]:
        start_date_raw = st.text_input(
            "Start date (YYYY-MM-DD)",
            placeholder="2024-01-01",
            key="uploads_inbox_start_date",
        ).strip()
    with date_cols[1]:
        end_date_raw = st.text_input(
            "End date (YYYY-MM-DD)",
            placeholder="2024-01-31",
            key="uploads_inbox_end_date",
        ).strip()

    start_date = _parse_date_input(start_date_raw)
    end_date = _parse_date_input(end_date_raw)

    if start_date_raw and not start_date:
        st.warning("Start date is invalid. Use YYYY-MM-DD.")
    if end_date_raw and not end_date:
        st.warning("End date is invalid. Use YYYY-MM-DD.")
    if start_date and end_date and end_date < start_date:
        st.warning("End date must be on or after the start date.")
        start_date = None
        end_date = None

    db = SessionLocal()
    try:
        query = db.query(UploadPortalFile)
        if completed_only:
            query = query.filter(UploadPortalFile.completed_at.isnot(None))
        if user_email_filter:
            query = query.filter(UploadPortalFile.user_email.ilike(f"%{user_email_filter}%"))
        if start_date:
            start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
            query = query.filter(UploadPortalFile.created_at >= start_dt)
        if end_date:
            end_dt = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
            query = query.filter(UploadPortalFile.created_at < end_dt)

        rows = (
            query.order_by(UploadPortalFile.created_at.desc())
            .limit(50)
            .all()
        )
        if not rows:
            st.info("No secure uploads recorded yet.")
            return

        items = []
        for row in rows:
            object_name = row.object_name or ""
            gcs_bucket = row.gcs_bucket or ""
            gs_path = f"gs://{gcs_bucket}/{object_name}" if gcs_bucket and object_name else None
            console_url = None
            if gcs_bucket and object_name:
                encoded_object = quote(object_name, safe="/")
                console_url = (
                    "https://console.cloud.google.com/storage/browser/_details/"
                    f"{gcs_bucket}/{encoded_object}"
                )
            items.append(
                {
                    "created_at": format_mst(row.created_at),
                    "completed_at": format_mst(row.completed_at),
                    "user_email": row.user_email,
                    "console_url": console_url,
                    "user_id": str(row.user_id) if row.user_id else None,
                    "original_filename": row.original_filename,
                    "content_type": row.content_type,
                    "byte_size": row.byte_size,
                    "gcs_bucket": row.gcs_bucket,
                    "object_name": row.object_name,
                    "request_id": str(row.request_id),
                    "session_id": str(row.session_id),
                    "gs_path": gs_path,
                }
            )

        st.dataframe(
            items,
            use_container_width=True,
            column_config={
                "console_url": st.column_config.LinkColumn("Console link", display_text="View file"),
            },
        )
    except Exception:
        st.info("Secure uploads table is not available yet. Complete database setup to view uploads.")
    finally:
        db.close()

def display_client_submissions():
    st.markdown("<h3 style='margin-top: 1.5rem;'>Client Submissions</h3>", unsafe_allow_html=True)
    search_term = st.text_input("Search by email", placeholder="Search by email")
    normalized_search = search_term.strip().lower() if search_term else ""
    
    db = next(get_db())
    try:
        query = db.query(
            ClientSubmission.user_email.label("email"),
            func.count(ClientSubmission.id).label("submission_count"),
            func.max(ClientSubmission.submitted_at).label("last_submitted_at"),
        )
        if normalized_search:
            query = query.filter(ClientSubmission.user_email.ilike(f"%{normalized_search}%"))
        clients = query.group_by(ClientSubmission.user_email).order_by(
            func.max(ClientSubmission.submitted_at).desc()
        ).all()
        
        total_submissions = sum(row.submission_count for row in clients)
        logging.info(
            "Dashboard query counts: clients=%d, submissions=%d",
            len(clients),
            total_submissions,
        )
        
        if not clients:
            st.write("No client submissions available")
            return
        
        header_cols = st.columns([3.6, 1.4, 2.2, 0.8])
        with header_cols[0]:
            st.markdown("**Email**")
        with header_cols[1]:
            st.markdown("**Submissions**")
        with header_cols[2]:
            st.markdown("**Last Submitted**")
        with header_cols[3]:
            st.markdown("**Delete**")
        
        st.markdown("<div style='margin-bottom: 0.5rem;'></div>", unsafe_allow_html=True)
        
        for client in clients:
            client_key = client.email.replace("@", "_at_").replace(".", "_")
            st.markdown('<div class="as-card">', unsafe_allow_html=True)
            cols = st.columns([3.6, 1.4, 2.2, 0.8])
            with cols[0]:
                st.markdown(f"[{client.email}](mailto:{client.email})")
            with cols[1]:
                st.markdown(f"<span class=\"as-pill\">{client.submission_count}</span>", unsafe_allow_html=True)
            with cols[2]:
                if client.last_submitted_at:
                    st.write(_format_admin_dt(client.last_submitted_at) or "-")
                else:
                    st.write("-")
            with cols[3]:
                if st.button("🗑️", key=f"delete_btn_{client_key}"):
                    st.session_state[f"confirm_delete_{client_key}"] = client.email
                    st.rerun()

            if st.session_state.get(f"confirm_delete_{client_key}"):
                st.warning(f"Are you sure you want to delete all records for {client.email}?")
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Yes, Delete", key=f"confirm_yes_{client_key}", type="primary"):
                        try:
                            delete_user(db, client.email)
                            st.success(f"Deleted all records for {client.email}")
                            del st.session_state[f"confirm_delete_{client_key}"]
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error deleting user: {str(e)}")
                with col2:
                    if st.button("Cancel", key=f"confirm_no_{client_key}"):
                        del st.session_state[f"confirm_delete_{client_key}"]
                        st.rerun()

            with st.expander(f"View submissions for {client.email}", expanded=False):
                submission_rows = db.query(
                    ClientSubmission,
                    func.count(Upload.id).label("upload_count"),
                ).outerjoin(
                    Upload, Upload.submission_id == ClientSubmission.id
                ).filter(
                    ClientSubmission.user_email == client.email
                ).group_by(
                    ClientSubmission.id
                ).order_by(
                    ClientSubmission.submitted_at.desc()
                ).all()

                if not submission_rows:
                    st.write("No submissions available for this client.")
                else:
                    submission_ids = [row[0].id for row in submission_rows]
                    uploads_by_submission = {}
                    if submission_ids:
                        uploads = db.query(Upload).filter(
                            Upload.submission_id.in_(submission_ids)
                        ).order_by(Upload.upload_time.desc()).all()
                        for upload in uploads:
                            uploads_by_submission.setdefault(upload.submission_id, []).append(upload)

                    for submission_index, submission_row in enumerate(submission_rows):
                        submission = submission_row[0]
                        upload_count = submission_row[1]
                        full_name = f"{submission.first_name} {submission.last_name}".strip()
                        submission_label = _format_admin_dt(submission.submitted_at) or "-"

                        st.markdown('<div class="as-subcard">', unsafe_allow_html=True)
                        sub_cols = st.columns([2.2, 2.2, 2.4, 1.6, 1.2])
                        with sub_cols[0]:
                            st.markdown(
                                f"<div class=\"as-muted\">Submitted At</div><div>{submission_label}</div>",
                                unsafe_allow_html=True,
                            )
                        with sub_cols[1]:
                            name_value = full_name if full_name.strip() else "-"
                            st.markdown(
                                f"<div class=\"as-muted\">Name</div><div>{name_value}</div>",
                                unsafe_allow_html=True,
                            )
                        with sub_cols[2]:
                            office_value = submission.office_name or "-"
                            st.markdown(
                                f"<div class=\"as-muted\">Office/Group</div><div>{office_value}</div>",
                                unsafe_allow_html=True,
                            )
                        with sub_cols[3]:
                            org_value = submission.org_type or "-"
                            st.markdown(
                                f"<div class=\"as-muted\">Org Type</div><div>{org_value}</div>",
                                unsafe_allow_html=True,
                            )
                        with sub_cols[4]:
                            st.markdown(
                                f"<div class=\"as-muted\">Uploads</div><span class=\"as-pill\">{upload_count}</span>",
                                unsafe_allow_html=True,
                            )

                        uploads_for_submission = uploads_by_submission.get(submission.id, [])
                        with st.expander(
                            f"Uploads for {submission_label} ({len(uploads_for_submission)})",
                            expanded=False,
                        ):
                            if not uploads_for_submission:
                                st.write("No uploads linked to this submission.")
                            else:
                                upload_header_cols = st.columns([3.0, 2.0, 2.2, 1.0, 1.0])
                                with upload_header_cols[0]:
                                    st.markdown("**File Name**")
                                with upload_header_cols[1]:
                                    st.markdown("**Tool**")
                                with upload_header_cols[2]:
                                    st.markdown("**Upload Time**")
                                with upload_header_cols[3]:
                                    st.markdown("**Summary**")
                                with upload_header_cols[4]:
                                    st.markdown("**Analysis**")

                                for upload in uploads_for_submission:
                                    upload_key = f"{submission.id}_{upload.id}"
                                    analysis_payload = _parse_analysis_json(upload.analysis_data)
                                    has_analysis = bool(analysis_payload)
                                    upload_cols = st.columns([3.0, 2.0, 2.2, 1.0, 1.0])
                                    with upload_cols[0]:
                                        st.write(upload.file_name or "-")
                                    with upload_cols[1]:
                                        st.write(upload.tool_name or "-")
                                    with upload_cols[2]:
                                        st.write(_format_admin_dt(upload.upload_time) or "-")
                                    with upload_cols[3]:
                                        if has_analysis:
                                            if st.button("📥", key=f"download_btn_{upload_key}"):
                                                st.session_state[f"show_summary_{upload_key}"] = True
                                                st.rerun()
                                        else:
                                            st.write("-")
                                    with upload_cols[4]:
                                        if has_analysis:
                                            if st.button("📄", key=f"view_btn_{upload_key}"):
                                                st.session_state[f"show_analysis_{upload_key}"] = True
                                                st.rerun()
                                        else:
                                            st.write("-")

                                    if st.session_state.get(f"show_summary_{upload_key}", False):
                                        st.markdown("---")
                                        st.markdown(
                                            f"**Admin Summary for {upload.file_name} ({upload.tool_name})**"
                                        )
                                        if analysis_payload:
                                            raw_analyses = analysis_payload.get("raw_analyses", {})
                                            if not isinstance(raw_analyses, dict):
                                                raw_analyses = {}
                                            total_issue_count = analysis_payload.get("total_issue_count", "N/A")
                                            openai_text = raw_analyses.get("OpenAI Analysis", "No OpenAI analysis available.")
                                            xai_text = raw_analyses.get("xAI Analysis", "No xAI analysis available.")
                                            anthropic_text = raw_analyses.get("AnthropicAI Analysis", "No Anthropic analysis available.")
                                            admin_summary = f"""
Tool: {upload.tool_name}
File Name: {upload.file_name}

Submitted by:
First Name: {submission.first_name}
Last Name: {submission.last_name}
Office/Group: {submission.office_name}
Email: {submission.user_email}
Organization Type: {submission.org_type}

Total Issues Identified: {total_issue_count}

=== OpenAI GPT-4 Analysis ===
{openai_text}

=== xAI Grok Analysis ===
{xai_text}

=== Anthropic Claude Analysis ===
{anthropic_text}
"""
                                        
                                        st.download_button(
                                            label="📥 Download Admin Summary",
                                            data=admin_summary,
                                            file_name=f"admin_summary_{submission.user_email}_{upload.file_name}.txt",
                                            mime="text/plain",
                                            key=f"download_summary_{upload_key}"
                                        )
                                        
                                        st.text_area(
                                            "Admin Summary",
                                            admin_summary,
                                            height=400,
                                            key=f"summary_text_{upload_key}",
                                            disabled=True
                                        )
                                        
                                        if st.button("Close", key=f"close_summary_{upload_key}"):
                                            st.session_state[f"show_summary_{upload_key}"] = False
                                            st.rerun()
                                        else:
                                            st.info("No analysis data available.")
                                            if st.button("Close", key=f"close_summary_{upload_key}"):
                                                st.session_state[f"show_summary_{upload_key}"] = False
                                                st.rerun()

                                    if st.session_state.get(f"show_analysis_{upload_key}", False):
                                        st.markdown("---")
                                        st.markdown(
                                            f"**Analysis for {upload.file_name} ({upload.tool_name})**"
                                        )
                                        if analysis_payload:
                                            raw_analyses = analysis_payload.get("raw_analyses", {})
                                            if not isinstance(raw_analyses, dict):
                                                raw_analyses = {}
                                            total_issue_count = analysis_payload.get("total_issue_count", "N/A")
                                            openai_text = raw_analyses.get("OpenAI Analysis", "No OpenAI analysis available.")
                                            xai_text = raw_analyses.get("xAI Analysis", "No xAI analysis available.")
                                            anthropic_text = raw_analyses.get("AnthropicAI Analysis", "No Anthropic analysis available.")
                                            st.markdown(f"**Total Issues Identified:** {total_issue_count}")

                                            st.markdown("**OpenAI Analysis:**")
                                            st.text_area(
                                                "",
                                                openai_text,
                                                height=200,
                                                key=f"openai_{upload_key}",
                                                disabled=True
                                            )

                                            st.markdown("**xAI Analysis:**")
                                            st.text_area(
                                                "",
                                                xai_text,
                                                height=200,
                                                key=f"xai_{upload_key}",
                                                disabled=True
                                            )

                                            st.markdown("**Anthropic Analysis:**")
                                            st.text_area(
                                                "",
                                                anthropic_text,
                                                height=200,
                                                key=f"anthropic_{upload_key}",
                                                disabled=True
                                            )

                                            if st.button("Close", key=f"close_{upload_key}"):
                                                st.session_state[f"show_analysis_{upload_key}"] = False
                                                st.rerun()
                                        else:
                                            st.info("No analysis data available.")
                                            if st.button("Close", key=f"close_{upload_key}"):
                                                st.session_state[f"show_analysis_{upload_key}"] = False
                                                st.rerun()

                        st.markdown("</div>", unsafe_allow_html=True)
                        if submission_index < len(submission_rows) - 1:
                            st.divider()

            st.markdown("</div>", unsafe_allow_html=True)
            st.markdown("<div style='height: 10px;'></div>", unsafe_allow_html=True)
            st.divider()
    finally:
        db.close()

def display_document_analysis():
    """Admin-only document analysis for AR and Insurance Claims"""
    st.markdown("<h3 style='margin-top: 1.5rem;'>Document Analysis</h3>", unsafe_allow_html=True)
    st.info("These analysis tools are available to admin users only. Upload AR or Insurance Claims documents for analysis.")
    
    # Import analysis functions from analysis_utils.py
    from analysis_utils import analyze_with_all_models, send_followup_email, send_email, extract_text_from_pdf
    import pandas as pd
    
    # Contact Information Form for admin analysis
    with st.form("admin_analysis_form"):
        st.markdown("**Client Information**")
        first_name = st.text_input("Client First Name", key="admin_first_name")
        last_name = st.text_input("Client Last Name", key="admin_last_name")
        office_name = st.text_input("Office/Group Name", key="admin_office_name")
        email = st.text_input("Client Email Address", placeholder="client@example.com", key="admin_email")
        org_type = st.selectbox("Type", ["Location", "Group"], key="admin_org_type")
        submit_info = st.form_submit_button("Save Client Info")
    
    import re
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    valid_email = re.match(email_pattern, email) if email else False
    
    client_info_complete = all([first_name, last_name, office_name, email, org_type]) and valid_email
    
    if email and not valid_email:
        st.error("Please enter a valid email address")
    
    if not client_info_complete:
        st.warning("Please complete the client information above before uploading documents.")
    else:
        st.markdown("---")
        st.markdown("**Upload Documents for Analysis**")
        
        # AR File Upload Section
        st.markdown("""
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 0.5rem; margin-top: 1rem;">
                <span style="font-size: 1.2rem;">📊</span>
                <span style="font-size: 1.1rem; font-weight: 500;">Accounts Receivable Analysis</span>
            </div>
        """, unsafe_allow_html=True)
        ar_file = st.file_uploader("Upload AR Report", type=["csv", "xlsx"], key="admin_ar")
        
        # Insurance Claims File Upload Section
        st.markdown("""
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 0.5rem; margin-top: 1rem;">
                <span style="font-size: 1.2rem;">📋</span>
                <span style="font-size: 1.1rem; font-weight: 500;">Insurance Claims Analysis</span>
            </div>
        """, unsafe_allow_html=True)
        claim_file = st.file_uploader("Upload Claim Report", type=["csv", "xlsx", "pdf"], key="admin_claim")
        
        # Analyze Button
        st.markdown("---")
        uploaded_files = {
            "AR Analyzer": ar_file,
            "Insurance Claim Analyzer": claim_file,
        }
        uploaded_count = sum(1 for f in uploaded_files.values() if f is not None)
        
        if uploaded_count > 0:
            st.markdown(f"**Documents ready for analysis:** {uploaded_count}")
            
            if 'admin_analyzing' not in st.session_state:
                st.session_state.admin_analyzing = False
            
            analyze_clicked = st.button("Analyze Documents", type="primary", disabled=st.session_state.admin_analyzing, key="admin_analyze_btn")
            
            if st.session_state.admin_analyzing:
                st.info("Analysis in progress. This may take a couple of minutes...")
            
            if analyze_clicked:
                st.session_state.admin_analyzing = True
                st.rerun()
            
            if st.session_state.admin_analyzing:
                normalized_email = normalize_email(email)
                logging.info("Normalized email: %s", normalized_email)
                user_info_dict = {
                    "first_name": first_name,
                    "last_name": last_name,
                    "office_name": office_name,
                    "email": normalized_email,
                    "org_type": org_type,
                }
                
                # Save user to database
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
                        db.commit()
                        logging.info("User upsert: created for %s (admin)", normalized_email)
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
                            db.commit()
                            logging.info("User upsert: updated for %s (admin)", normalized_email)
                        else:
                            logging.info("User upsert: existing for %s (admin)", normalized_email)
                except Exception as e:
                    logging.error(f"Error saving user: {str(e)}")
                    db.rollback()
                finally:
                    db.close()
                
                # Process each uploaded document
                analysis_results = {}
                upload_ids = []
                all_emails_sent = True
                for tool_name, file in uploaded_files.items():
                    if file is not None:
                        with st.spinner(f"Analyzing {tool_name}..."):
                            file.seek(0)
                            # Read file content
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

                            file.seek(0)
                            
                            # Extract text from file
                            if file_type == "application/pdf":
                                text = extract_text_from_pdf(file)
                            elif file_type in ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel"]:
                                df = pd.read_excel(file)
                                text = df.to_string()
                            else:
                                df = pd.read_csv(file)
                                text = df.to_string()
                            
                            # Run analysis
                            results = analyze_with_all_models(text)
                            analysis_results[tool_name] = results
                            
                            # Send emails
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
                            if not email_success:
                                all_emails_sent = False
                            
                            # Save upload to database
                            upload_db = SessionLocal()
                            try:
                                analysis_json = json.dumps({
                                    'raw_analyses': results['raw_analyses'],
                                    'deduplicated_issues': results['deduplicated_issues'],
                                    'total_issue_count': results['total_issue_count'],
                                    'all_trends': results.get('all_trends', [])
                                })
                                new_upload = Upload(
                                    file_name=file_name,
                                    tool_name=tool_name,
                                    upload_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    user_email=normalized_email,
                                    analysis_data=analysis_json
                                )
                                upload_db.add(new_upload)
                                upload_db.commit()
                                logging.info(f"Upload saved (admin): {file_name}")

                                update_upload_file_upload_id(upload_file_id, new_upload.id)
                                upload_ids.append(new_upload.id)
                            except Exception as e:
                                logging.error(f"Error saving upload: {str(e)}")
                                upload_db.rollback()
                            finally:
                                upload_db.close()
                
                if upload_ids and all_emails_sent:
                    submission_db = SessionLocal()
                    try:
                        submission = ClientSubmission(
                            user_email=normalized_email,
                            first_name=first_name,
                            last_name=last_name,
                            office_name=office_name,
                            org_type=org_type,
                        )
                        submission_db.add(submission)
                        submission_db.commit()
                        submission_db.refresh(submission)
                        logging.info(
                            "Submission snapshot created: %s for %s (admin)",
                            submission.id,
                            normalized_email,
                        )
                        
                        submission_db.query(Upload).filter(Upload.id.in_(upload_ids)).update(
                            {"submission_id": submission.id},
                            synchronize_session=False
                        )
                        submission_db.commit()
                        logging.info(
                            "Linked %d uploads to submission_id %s (admin)",
                            len(upload_ids),
                            submission.id,
                        )
                    except Exception as e:
                        logging.error(
                            "Error creating submission snapshot for %s (admin): %s",
                            normalized_email,
                            str(e),
                        )
                        submission_db.rollback()
                    finally:
                        submission_db.close()
                elif upload_ids and not all_emails_sent:
                    logging.warning(
                        "Submission snapshot skipped for %s due to email failure (admin)",
                        normalized_email,
                    )
                
                st.session_state.admin_analyzing = False
                st.success("Analysis complete! Results have been emailed to the client and admin team.")
                st.rerun()
        else:
            st.info("Upload an AR Report or Insurance Claims document to begin analysis.")

def display_admin_management():
    st.markdown("<h3 style='margin-top: 1.5rem;'>Admin Management</h3>", unsafe_allow_html=True)
    st.info("Admin access is managed through Supabase Auth and the admin_users table.")
