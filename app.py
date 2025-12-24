import streamlit as st

# ## UPLOAD_PORTAL_MOUNT
# Mount upload portal routes onto Streamlit's Tornado server.
try:
    from streamlit.web.server.server import Server
    from upload_portal_routes import register_upload_portal_routes

    _server = Server.get_current()
    _tornado = getattr(_server, "_tornado", None)
    if _tornado is not None:
        register_upload_portal_routes(_tornado)
except Exception:
    # Avoid breaking Streamlit if internals differ; errors show up in Render logs.
    pass

import pandas as pd
import requests
from PIL import Image
import pymupdf as fitz
import pytesseract
import tempfile
import os
from io import BytesIO
import hmac
import time
import logging
from database import get_db, Base, engine, SessionLocal
from models import get_admin_by_username, Admin, create_admin, User, Upload, ClientSubmission
from supabase_utils import (
    get_admin_user_count,
    get_current_admin_user,
    is_admin_user,
    persist_upload_file,
    sign_in_admin,
    update_upload_file_upload_id,
)
from datetime import datetime
from analysis_utils import (
    extract_text_from_pdf,
    analyze_with_all_models,
    send_followup_email,
    send_email,
    categorize_issue,
    extract_compelling_insights
)
from admin_dashboard import display_admin_dashboard
from upload_portal_routes import register_upload_portal_routes

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def normalize_email(raw_email: str) -> str:
    if not raw_email:
        return ""
    return raw_email.strip().lower()

# ---- API Keys ----
# API keys are loaded from environment variables (Replit Secrets)
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

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

# ---- Initialize Session State ----
if 'analysis_complete' not in st.session_state:
    st.session_state.analysis_complete = False
if 'analysis_results' not in st.session_state:
    st.session_state.analysis_results = {}
if 'is_admin_logged_in' not in st.session_state:
    st.session_state.is_admin_logged_in = False
if 'admin_session' not in st.session_state:
    st.session_state.admin_session = None
if 'admin_user' not in st.session_state:
    st.session_state.admin_user = None

# ---- Page Navigation (no sidebar, using session state) ----
if 'page' not in st.session_state:
    st.session_state.page = "Analyzer"

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
            st.rerun()
    
    else:
        # Show contact form and upload sections only if analysis is not complete
        # Contact Information Form
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
            first_name = st.text_input("First Name")
            last_name = st.text_input("Last Name")
            office_name = st.text_input("Office/Group Name")
            email = st.text_input("Email Address", placeholder="user@example.com")
            org_type = st.selectbox("Type", ["Location", "Group"])
            submit_user_info = st.form_submit_button("Save Info")

        import re
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        valid_email = re.match(email_pattern, email) if email else False
        
        user_info_complete = all([first_name, last_name, office_name, email, org_type]) and valid_email
        
        if email and not valid_email:
            st.error("Please enter a valid email address (e.g., user@example.com)")
        
        if not user_info_complete:
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
            pnl_file = st.file_uploader("Upload your financial document", type=["xlsx", "csv", "pdf"], key="pnl", label_visibility="collapsed")
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
                
                analyze_clicked = st.button("Analyze Document", type="primary", disabled=st.session_state.analyzing)
                
                if st.session_state.analyzing:
                    st.markdown("""
                        <div style="display: flex; align-items: center; gap: 10px; margin-top: 1rem;">
                            <div class="spinner"></div>
                            <span style="color: #AD8BF7; font-size: 0.95rem;">Analysis may take a couple of minutes. Please wait...</span>
                        </div>
                    """, unsafe_allow_html=True)
                
                if analyze_clicked:
                    st.session_state.analyzing = True
                    st.rerun()
                
                if st.session_state.analyzing:
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
                                st.session_state.debug_log.append(f"🔍 Processing file: {file.name} ({tool_name})")
                                with st.spinner(f"Analyzing {tool_name}..."):
                                    # Read file content once upfront to avoid invalidating file object
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
                                    
                                    # Reset file pointer for processing
                                    file.seek(0)
                                    
                                    if file.name.endswith(".pdf"):
                                        raw_text = extract_text_from_pdf(file)
                                        data_input = raw_text
                                    else:
                                        df = pd.read_csv(file) if file.name.endswith(".csv") else pd.read_excel(file)
                                        data_input = df.to_string(index=False)
                                    
                                    st.session_state.debug_log.append(f"🔍 Running AI analysis for {file.name}...")
                                    # Run the analysis with all models
                                    results = analyze_with_all_models(data_input)
                                    st.session_state.debug_log.append(f"✅ Analysis complete for {file.name}")
                                    
                                    # Store results in session state
                                    st.session_state.analysis_results[tool_name] = results
                                    
                                    st.session_state.debug_log.append(f"📧 Sending emails for {file.name}...")
                                    # Send emails (pass file content instead of file object)
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
                                    
                                    # Save upload to database with FRESH session (after long AI analysis)
                                    st.session_state.debug_log.append(f"💾 Opening new database session for {file.name}...")
                                    upload_db = SessionLocal()
                                    try:
                                        import json
                                        logging.info(f"Starting database save for {file.name}")
                                        
                                        st.session_state.debug_log.append(f"🔍 Serializing analysis to JSON...")
                                        # Include all analysis data including trends
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
                                        upload_db.commit()
                                        st.session_state.debug_log.append(f"✅ Database commit successful!")
                                        logging.info(f"✅ Upload committed successfully: {file.name} - {tool_name}")

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
                                        # Always close the upload database session
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
                                "Submission snapshot created: %s for %s",
                                submission.id,
                                normalized_email,
                            )
                            
                            submission_db.query(Upload).filter(Upload.id.in_(upload_ids)).update(
                                {"submission_id": submission.id},
                                synchronize_session=False
                            )
                            submission_db.commit()
                            logging.info(
                                "Linked %d uploads to submission_id %s",
                                len(upload_ids),
                                submission.id,
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
                    
                    # Reset analyzing state and mark analysis as complete
                    st.session_state.analyzing = False
                    st.session_state.analysis_complete = True
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
    admin_access_token = None
    if st.session_state.admin_session:
        admin_access_token = st.session_state.admin_session.get("access_token")
    if admin_access_token:
        admin_user = get_current_admin_user(admin_access_token)
        if admin_user:
            st.session_state.admin_user = admin_user
            st.session_state.is_admin_logged_in = is_admin_user(admin_user.get("id"))
        else:
            st.session_state.admin_session = None
            st.session_state.admin_user = None
            st.session_state.is_admin_logged_in = False

    # Check if admin is logged in
    if not st.session_state.is_admin_logged_in:
        # Show login form
        st.markdown("""
            <div class="title-container" style="margin-top: 1.5rem;">
                <h1>Admin Dashboard</h1>
            </div>
        """, unsafe_allow_html=True)
        st.markdown("<p style='text-align: center; margin-bottom: 2rem; margin-top: 1.5rem;'>Please log in to access the admin dashboard.</p>", unsafe_allow_html=True)

        admin_user_count = get_admin_user_count()
        if admin_user_count == 0:
            st.info("Ask Jason to add your auth user_id to admin_users.")
        
        # Login form
        with st.form("admin_login_form"):
            st.markdown("""
                <div class="section-header">
                    <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
                        <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
                    </svg>
                    <span class="section-title">Login</span>
                </div>
            """, unsafe_allow_html=True)
            
            email = st.text_input("Email", key="admin_email")
            password = st.text_input("Password", type="password", key="admin_password")
            
            col1, col2, col3 = st.columns([1, 1, 1])
            with col2:
                submit_button = st.form_submit_button("Login", use_container_width=True)
            
            if submit_button:
                auth_result, error = sign_in_admin(email, password)
                if error:
                    if "admin_password" in st.session_state:
                        del st.session_state["admin_password"]
                    st.error(f"Login failed: {error}")
                else:
                    st.session_state.admin_session = {
                        "access_token": auth_result["access_token"],
                        "refresh_token": auth_result.get("refresh_token"),
                    }
                    st.session_state.admin_user = auth_result.get("user")

                    if is_admin_user(st.session_state.admin_user.get("id")):
                        st.session_state.is_admin_logged_in = True
                        if "admin_email" in st.session_state:
                            del st.session_state["admin_email"]
                        if "admin_password" in st.session_state:
                            del st.session_state["admin_password"]
                        st.success("Login successful! Redirecting...")
                        st.rerun()
                    else:
                        st.session_state.is_admin_logged_in = False
                        st.error("Not authorized")

        if st.session_state.admin_user and not st.session_state.is_admin_logged_in:
            col1, col2, col3 = st.columns([1, 1, 1])
            with col2:
                if st.button("Logout", key="admin_logout_unauthorized", use_container_width=True):
                    st.session_state.admin_session = None
                    st.session_state.admin_user = None
                    st.session_state.is_admin_logged_in = False
                    st.rerun()
        
        # Back to analyzer button
        st.markdown("<br>", unsafe_allow_html=True)
        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            if st.button("Back to Analyzer", key="back_to_analyzer_from_login", use_container_width=True):
                st.session_state.page = "Analyzer"
                st.rerun()
    else:
        # Admin is logged in, show the dashboard
        display_admin_dashboard()  # This calls the function defined in admin_dashboard.py

# ---- Footer ----
st.markdown("""<hr style="margin-top: 3rem;">""", unsafe_allow_html=True)

# Admin links (only on Analyzer page)
if st.session_state.page == "Analyzer":
    col1, col2, col3 = st.columns([1,1,1])
    with col2:
        if st.button("Admin Dashboard", key="admin_link", use_container_width=True):
            st.session_state.page = "Admin Dashboard"
            st.rerun()

st.markdown("<p style='text-align: center; margin-top: 1rem;'>Built by <a href='https://alphasourceai.com' target='_blank'>AlphaSource AI</a></p>", unsafe_allow_html=True)
