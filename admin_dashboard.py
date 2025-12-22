import streamlit as st
import json
import os
from sqlalchemy.orm import Session
from models import get_db, get_users, get_uploads, delete_user, get_uploads_by_email, create_admin, update_admin_password, get_admin_by_username, get_all_admins, delete_admin, Admin, User, Upload
from database import SessionLocal
import logging
from datetime import datetime

# Admin Dashboard Page
def display_admin_dashboard():
    # Check if admin must change password
    if st.session_state.get("admin_data", {}).get("must_change_password", False):
        display_force_password_change()
        return
    
    # Header with logout button
    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown("<h1 style='margin-top: 1.5rem;'>Admin Dashboard</h1>", unsafe_allow_html=True)
    with col2:
        if st.button("Logout", key="logout_button", type="secondary"):
            st.session_state.is_admin_logged_in = False
            if "admin_data" in st.session_state:
                del st.session_state["admin_data"]
            st.session_state.page = "Analyzer"
            st.rerun()
    
    # Tab navigation
    tab1, tab2, tab3 = st.tabs(["Client Submissions", "Document Analysis", "Admin Management"])
    
    with tab1:
        display_client_submissions()
    
    with tab2:
        display_document_analysis()
    
    with tab3:
        display_admin_management()

def display_client_submissions():
    # Add compact styling CSS - targets first tab's content area only
    st.markdown("""
    <style>
    /* Compact table styling for Client Submissions tab - use data attribute targeting */
    [data-baseweb="tab-panel"]:first-of-type [data-testid="column"] p {
        font-size: 0.75rem !important;
        line-height: 1.2 !important;
        margin: 0.15rem 0 !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
    }
    
    /* Headers in the table */
    [data-baseweb="tab-panel"]:first-of-type [data-testid="column"] .stMarkdown strong {
        font-size: 0.8rem !important;
        font-weight: 600 !important;
    }
    
    /* Icon-only buttons - very compact */
    [data-baseweb="tab-panel"]:first-of-type .stButton > button {
        padding: 0.15rem 0.3rem !important;
        font-size: 1rem !important;
        min-height: 1.6rem !important;
        height: 1.6rem !important;
        line-height: 1 !important;
        border-radius: 4px !important;
    }
    
    /* Column spacing */
    [data-baseweb="tab-panel"]:first-of-type [data-testid="column"] {
        padding: 0.1rem 0.3rem !important;
    }
    
    /* Exclude modals and dialogs from compact styling */
    [data-baseweb="tab-panel"]:first-of-type [data-testid="stTextArea"] p,
    [data-baseweb="tab-panel"]:first-of-type .stAlert p,
    [data-baseweb="tab-panel"]:first-of-type .stWarning p,
    [data-baseweb="tab-panel"]:first-of-type .stError p,
    [data-baseweb="tab-panel"]:first-of-type .stSuccess p,
    [data-baseweb="tab-panel"]:first-of-type .stInfo p {
        font-size: 1rem !important;
        white-space: normal !important;
    }
    
    </style>
    """, unsafe_allow_html=True)
    
    st.markdown("<h3 style='margin-top: 1.5rem;'>Client Submissions</h3>", unsafe_allow_html=True)
    
    # Fetch users from database first
    db = next(get_db())
    try:
        users = get_users(db)
        
        if not users:
            st.write("No client data available")
            return
        
        # Build combined data structure
        combined_data = []
        for user in users:
            uploads = get_uploads_by_email(db, user.email)
            
            # Combine first and last name
            full_name = f"{user.first_name} {user.last_name}".strip()
            
            if uploads:
                for upload in uploads:
                    combined_data.append({
                        'name': full_name,
                        'email': user.email,
                        'office_name': user.office_name,
                        'org_type': user.org_type,
                        'file_name': upload.file_name,
                        'tool_name': upload.tool_name,
                        'upload_time': upload.upload_time,
                        'analysis_data': upload.analysis_data,
                        'first_name': user.first_name,
                        'last_name': user.last_name
                    })
            else:
                combined_data.append({
                    'name': full_name,
                    'email': user.email,
                    'office_name': user.office_name,
                    'org_type': user.org_type,
                    'file_name': None,
                    'tool_name': None,
                    'upload_time': None,
                    'analysis_data': None,
                    'first_name': user.first_name,
                    'last_name': user.last_name
                })
        
        # Table headers (outside the user loop)
        header_cols = st.columns([3.2, 3.0, 2.4, 1.8, 1.2, 1.2, 1.2])
        with header_cols[0]:
            st.markdown("**Name**")
        with header_cols[1]:
            st.markdown("**Email**")
        with header_cols[2]:
            st.markdown("**Office/Group**")
        with header_cols[3]:
            st.markdown("**Org Type**")
        
        st.markdown("<div style='margin-bottom: 0.5rem;'></div>", unsafe_allow_html=True)
        
        # Display table with action buttons (outside the user loop)
        for idx, data in enumerate(combined_data):
            with st.container():
                cols = st.columns([3.2, 3.0, 2.4, 1.8, 1.2, 1.2, 1.2])
                
                with cols[0]:
                    st.write(data['name'])
                with cols[1]:
                    st.write(data['email'])
                with cols[2]:
                    st.write(data['office_name'])
                with cols[3]:
                    st.write(data['org_type'])
                with cols[4]:
                    if data['analysis_data']:
                        if st.button("📥", key=f"download_btn_{idx}"):
                            st.session_state[f'show_summary_{idx}'] = True
                            st.rerun()
                    else:
                        st.write("-")
                with cols[5]:
                    if data['analysis_data']:
                        if st.button("📄", key=f"view_btn_{idx}"):
                            st.session_state[f'show_analysis_{idx}'] = True
                            st.rerun()
                    else:
                        st.write("-")
                with cols[6]:
                    if st.button("🗑️", key=f"delete_btn_{idx}"):
                        st.session_state[f'confirm_delete_{idx}'] = data['email']
                        st.rerun()
                
                # Show admin summary modal
                if st.session_state.get(f'show_summary_{idx}', False):
                    st.markdown("---")
                    st.markdown(f"**Admin Summary for {data['file_name']} ({data['tool_name']})**")
                    
                    try:
                        analysis = json.loads(data['analysis_data'])
                        
                        # Generate admin summary email content
                        admin_summary = f"""
Tool: {data['tool_name']}
File Name: {data['file_name']}

Submitted by:
First Name: {data['first_name']}
Last Name: {data['last_name']}
Office/Group: {data['office_name']}
Email: {data['email']}
Organization Type: {data['org_type']}

Total Issues Identified: {analysis['total_issue_count']}

=== OpenAI GPT-4 Analysis ===
{analysis['raw_analyses']['OpenAI Analysis']}

=== xAI Grok Analysis ===
{analysis['raw_analyses']['xAI Analysis']}

=== Anthropic Claude Analysis ===
{analysis['raw_analyses']['AnthropicAI Analysis']}
"""
                        
                        st.download_button(
                            label="📥 Download Admin Summary",
                            data=admin_summary,
                            file_name=f"admin_summary_{data['email']}_{data['file_name']}.txt",
                            mime="text/plain",
                            key=f"download_summary_{idx}"
                        )
                        
                        st.text_area("Admin Summary", admin_summary, height=400, key=f"summary_text_{idx}", disabled=True)
                        
                        if st.button("Close", key=f"close_summary_{idx}"):
                            st.session_state[f'show_summary_{idx}'] = False
                            st.rerun()
                    except Exception as e:
                        st.error(f"Error displaying admin summary: {str(e)}")
                
                # Show analysis modal
                if st.session_state.get(f'show_analysis_{idx}', False):
                    st.markdown("---")
                    st.markdown(f"**Analysis for {data['file_name']} ({data['tool_name']})**")
                    
                    try:
                        analysis = json.loads(data['analysis_data'])
                        
                        st.markdown(f"**Total Issues Identified:** {analysis['total_issue_count']}")
                        
                        st.markdown("**OpenAI Analysis:**")
                        st.text_area("", analysis['raw_analyses']['OpenAI Analysis'], height=200, key=f"openai_{idx}", disabled=True)
                        
                        st.markdown("**xAI Analysis:**")
                        st.text_area("", analysis['raw_analyses']['xAI Analysis'], height=200, key=f"xai_{idx}", disabled=True)
                        
                        st.markdown("**Anthropic Analysis:**")
                        st.text_area("", analysis['raw_analyses']['AnthropicAI Analysis'], height=200, key=f"anthropic_{idx}", disabled=True)
                        
                        if st.button("Close", key=f"close_{idx}"):
                            st.session_state[f'show_analysis_{idx}'] = False
                            st.rerun()
                    except Exception as e:
                        st.error(f"Error displaying analysis: {str(e)}")
                
                # Confirm deletion
                if st.session_state.get(f'confirm_delete_{idx}'):
                    st.warning(f"Are you sure you want to delete all records for {data['email']}?")
                    col1, col2 = st.columns(2)
                    with col1:
                        if st.button("Yes, Delete", key=f"confirm_yes_{idx}", type="primary"):
                            try:
                                delete_user(db, data['email'])
                                st.success(f"Deleted all records for {data['email']}")
                                del st.session_state[f'confirm_delete_{idx}']
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error deleting user: {str(e)}")
                    with col2:
                        if st.button("Cancel", key=f"confirm_no_{idx}"):
                            del st.session_state[f'confirm_delete_{idx}']
                            st.rerun()
                
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
                user_info_dict = {
                    "first_name": first_name,
                    "last_name": last_name,
                    "office_name": office_name,
                    "email": email,
                    "org_type": org_type,
                }
                
                # Save user to database
                db = SessionLocal()
                try:
                    existing_user = db.query(User).filter(User.email == email).first()
                    if not existing_user:
                        new_user = User(
                            first_name=first_name,
                            last_name=last_name,
                            email=email,
                            office_name=office_name,
                            org_type=org_type
                        )
                        db.add(new_user)
                        db.commit()
                        logging.info(f"New user created (admin): {email}")
                except Exception as e:
                    logging.error(f"Error saving user: {str(e)}")
                    db.rollback()
                finally:
                    db.close()
                
                # Process each uploaded document
                analysis_results = {}
                for tool_name, file in uploaded_files.items():
                    if file is not None:
                        with st.spinner(f"Analyzing {tool_name}..."):
                            # Read file content
                            file_content = file.read()
                            file.seek(0)
                            file_name = file.name
                            file_type = file.type
                            
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
                            send_followup_email(user_info_dict, tool_name, results)
                            send_email(user_info_dict, file_content, file_name, file_type, results, tool_name)
                            
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
                                    user_email=email,
                                    analysis_data=analysis_json
                                )
                                upload_db.add(new_upload)
                                upload_db.commit()
                                logging.info(f"Upload saved (admin): {file_name}")
                            except Exception as e:
                                logging.error(f"Error saving upload: {str(e)}")
                                upload_db.rollback()
                            finally:
                                upload_db.close()
                
                st.session_state.admin_analyzing = False
                st.success("Analysis complete! Results have been emailed to the client and admin team.")
                st.rerun()
        else:
            st.info("Upload an AR Report or Insurance Claims document to begin analysis.")

def display_admin_management():
    st.markdown("<h3 style='margin-top: 1.5rem;'>Admin Management</h3>", unsafe_allow_html=True)
    
    # Get current admin info
    current_admin = st.session_state.get("admin_data", {})
    current_username = current_admin.get("username", "")
    
    # Change Password Section
    st.markdown("#### Change Your Password")
    with st.form("change_password_form"):
        new_password = st.text_input("New Password", type="password")
        confirm_password = st.text_input("Confirm New Password", type="password")
        submit_change = st.form_submit_button("Change Password")
        
        if submit_change:
            if not new_password or not confirm_password:
                st.error("Please fill in all fields")
            elif new_password != confirm_password:
                st.error("Passwords do not match")
            elif len(new_password) < 8:
                st.error("Password must be at least 8 characters long")
            else:
                db = next(get_db())
                try:
                    if update_admin_password(db, current_username, new_password):
                        st.success("Password changed successfully!")
                    else:
                        st.error("Failed to change password")
                finally:
                    db.close()
    
    st.divider()
    
    # Create New Admin Section
    st.markdown("#### Create New Admin User")
    st.info("New admins will be required to change their temporary password on first login")
    
    with st.form("create_admin_form"):
        new_admin_username = st.text_input("Username")
        new_admin_email = st.text_input("Email")
        temp_password = st.text_input("Temporary Password", type="password")
        submit_create = st.form_submit_button("Create Admin")
        
        if submit_create:
            if not new_admin_username or not new_admin_email or not temp_password:
                st.error("Please fill in all fields")
            elif len(temp_password) < 8:
                st.error("Password must be at least 8 characters long")
            else:
                db = next(get_db())
                try:
                    existing_admin = get_admin_by_username(db, new_admin_username)
                    if existing_admin:
                        st.error(f"Admin user '{new_admin_username}' already exists")
                    else:
                        create_admin(db, new_admin_username, temp_password, new_admin_email, must_change_password=True)
                        st.success(f"Admin user '{new_admin_username}' created successfully! They must change their password on first login.")
                        logging.info(f"New admin created: {new_admin_username} by {current_username}")
                except Exception as e:
                    st.error(f"Error creating admin: {str(e)}")
                    logging.error(f"Error creating admin: {str(e)}")
                finally:
                    db.close()
    
    st.divider()
    
    # List existing admins
    st.markdown("#### Existing Admin Accounts")
    db = next(get_db())
    try:
        admins = get_all_admins(db)
        admin_count = len(admins)
        
        if admins:
            for idx, admin in enumerate(admins):
                col1, col2, col3 = st.columns([4, 2, 1])
                
                with col1:
                    status = "⚠️ Must change password" if admin.must_change_password else "✅ Active"
                    is_current = "(You)" if admin.username == current_username else ""
                    st.write(f"**{admin.username}** {is_current}")
                    st.caption(f"{admin.email or 'No email'} - {status}")
                
                with col2:
                    pass
                
                with col3:
                    is_self = admin.username == current_username
                    is_last = admin_count <= 1
                    
                    if is_self:
                        st.button("🗑️", key=f"del_admin_{idx}", disabled=True, help="Cannot delete your own account")
                    elif is_last:
                        st.button("🗑️", key=f"del_admin_{idx}", disabled=True, help="Cannot delete the last admin")
                    else:
                        if st.button("🗑️", key=f"del_admin_{idx}", help=f"Delete {admin.username}"):
                            st.session_state[f'confirm_delete_admin_{admin.username}'] = True
                
                if st.session_state.get(f'confirm_delete_admin_{admin.username}'):
                    st.warning(f"Are you sure you want to delete admin **{admin.username}**? This action cannot be undone.")
                    confirm_col1, confirm_col2 = st.columns(2)
                    with confirm_col1:
                        if st.button("Yes, Delete", key=f"confirm_yes_admin_{idx}", type="primary"):
                            delete_db = next(get_db())
                            try:
                                success, message = delete_admin(delete_db, admin.username, current_username)
                                if success:
                                    st.success(message)
                                    logging.info(f"Admin '{admin.username}' deleted by {current_username}")
                                    del st.session_state[f'confirm_delete_admin_{admin.username}']
                                    st.rerun()
                                else:
                                    st.error(message)
                            except Exception as e:
                                st.error(f"Error deleting admin: {str(e)}")
                                logging.error(f"Error deleting admin {admin.username}: {str(e)}")
                            finally:
                                delete_db.close()
                    with confirm_col2:
                        if st.button("Cancel", key=f"confirm_no_admin_{idx}"):
                            del st.session_state[f'confirm_delete_admin_{admin.username}']
                            st.rerun()
                
                st.divider()
        else:
            st.write("No admin accounts found")
    finally:
        db.close()

def display_force_password_change():
    st.markdown("<h1 style='margin-top: 1.5rem;'>Password Change Required</h1>", unsafe_allow_html=True)
    st.warning("You must change your password before accessing the dashboard.")
    
    current_admin = st.session_state.get("admin_data", {})
    current_username = current_admin.get("username", "")
    
    with st.form("force_password_change_form"):
        new_password = st.text_input("New Password", type="password")
        confirm_password = st.text_input("Confirm New Password", type="password")
        submit = st.form_submit_button("Change Password")
        
        if submit:
            if not new_password or not confirm_password:
                st.error("Please fill in all fields")
            elif new_password != confirm_password:
                st.error("Passwords do not match")
            elif len(new_password) < 8:
                st.error("Password must be at least 8 characters long")
            else:
                db = next(get_db())
                try:
                    if update_admin_password(db, current_username, new_password, must_change=False):
                        logging.info(f"Forced password change completed for admin: {current_username}")
                        st.success("Password changed successfully! Redirecting to dashboard...")
                        st.session_state.admin_data["must_change_password"] = False
                        st.rerun()
                    else:
                        st.error("Failed to change password")
                except Exception as e:
                    logging.error(f"Error during forced password change for {current_username}: {str(e)}")
                    st.error(f"Error: {str(e)}")
                finally:
                    db.close()
