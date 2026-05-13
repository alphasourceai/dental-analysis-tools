import os
import bcrypt
from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from database import Base, engine, get_db

# User model
class User(Base):
    __tablename__ = "users"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    first_name = Column(String(255), index=True)
    last_name = Column(String(255), index=True)
    email = Column(String(255), unique=True, index=True)
    office_name = Column(String(255))
    org_type = Column(String(50))
    phone = Column(String(50), nullable=True)
    stripe_customer_id = Column(Text, nullable=True, unique=True, index=True)

# Client submission snapshot model
class ClientSubmission(Base):
    __tablename__ = "client_submissions"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_email = Column(Text, nullable=False, index=True)
    first_name = Column(String(255))
    last_name = Column(String(255))
    office_name = Column(String(255))
    org_type = Column(String(50))
    phone = Column(String(50), nullable=True)
    financial_only_acknowledgement = Column(Boolean, nullable=True)
    acknowledgement_timestamp = Column(DateTime(timezone=True), nullable=True)
    acknowledgement_ip = Column(Text, nullable=True)
    acknowledgement_version = Column(String(100), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True)
    source = Column(Text, nullable=True, index=True)
    status = Column(Text, nullable=True, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    canceled_at = Column(DateTime(timezone=True), nullable=True)
    errored_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    analysis_run_id = Column(Text, nullable=True, index=True)
    ghl_cid = Column(Text, nullable=True)
    ghl_analyzer_submitted_at = Column(DateTime(timezone=True), nullable=True)
    ghl_analyzer_submitted_error = Column(Text, nullable=True)

# Function to get users from the DB
def get_users(db):
    return db.query(User).all()

_SUBMISSION_UNSET = object()

def update_submission_status(
    db,
    submission_id,
    status=_SUBMISSION_UNSET,
    completed_at=_SUBMISSION_UNSET,
    canceled_at=_SUBMISSION_UNSET,
    errored_at=_SUBMISSION_UNSET,
    error_message=_SUBMISSION_UNSET,
    analysis_run_id=_SUBMISSION_UNSET,
) -> None:
    fields = []
    params = {"id": str(submission_id)}
    if status is not _SUBMISSION_UNSET:
        fields.append("status = :status")
        params["status"] = status
    if completed_at is not _SUBMISSION_UNSET:
        fields.append("completed_at = :completed_at")
        params["completed_at"] = completed_at
    if canceled_at is not _SUBMISSION_UNSET:
        fields.append("canceled_at = :canceled_at")
        params["canceled_at"] = canceled_at
    if errored_at is not _SUBMISSION_UNSET:
        fields.append("errored_at = :errored_at")
        params["errored_at"] = errored_at
    if error_message is not _SUBMISSION_UNSET:
        fields.append("error_message = :error_message")
        params["error_message"] = error_message
    if analysis_run_id is not _SUBMISSION_UNSET:
        fields.append("analysis_run_id = :analysis_run_id")
        params["analysis_run_id"] = analysis_run_id
    if not fields:
        return
    stmt = text(f"update client_submissions set {', '.join(fields)} where id = :id")
    db.execute(stmt, params)
    db.commit()

# Upload model
class Upload(Base):
    __tablename__ = "uploads"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"), index=True)
    file_name = Column(String(255))
    tool_name = Column(String(100))
    upload_time = Column(String(100))
    user_email = Column(String(255), index=True)
    analysis_data = Column(Text)
    submission_id = Column(PGUUID(as_uuid=True), nullable=True, index=True)
    paid = Column(Boolean, nullable=False, server_default=text("false"))
    pdf_version = Column(Integer, nullable=False, server_default=text("0"))
    pdf_url = Column(Text, nullable=True)
    pdf_generated_at = Column(DateTime(timezone=True), nullable=True)
    voided_at = Column(DateTime(timezone=True), nullable=True)
    voided_by_admin_user_id = Column(Text, nullable=True)
    voided_by_admin_email = Column(Text, nullable=True)
    void_reason = Column(Text, nullable=True)

# Stripe webhook/event audit model
class StripeEvent(Base):
    __tablename__ = "stripe_events"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    stripe_event_id = Column(Text, nullable=False, unique=True, index=True)
    event_type = Column(Text, nullable=False, index=True)
    livemode = Column(Boolean, nullable=False, server_default=text("false"))
    api_version = Column(Text, nullable=True)
    processing_status = Column(String(50), nullable=False, server_default=text("'received'"), index=True)
    error_message = Column(Text, nullable=True)
    received_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    payload = Column(Text, nullable=True)

# Stripe customer link model
class StripeCustomer(Base):
    __tablename__ = "stripe_customers"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    client_email = Column(Text, nullable=True, index=True)
    stripe_customer_id = Column(Text, nullable=True, unique=True, index=True)
    livemode = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

# Stripe Checkout Session audit/link model
class StripeCheckoutSession(Base):
    __tablename__ = "stripe_checkout_sessions"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    stripe_checkout_session_id = Column(Text, nullable=True, unique=True, index=True)
    stripe_customer_id = Column(Text, nullable=True, index=True)
    client_email = Column(Text, nullable=True, index=True)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    client_submission_id = Column(PGUUID(as_uuid=True), ForeignKey("client_submissions.id"), nullable=True, index=True)
    upload_id = Column(PGUUID(as_uuid=True), ForeignKey("uploads.id"), nullable=True, index=True)
    purpose = Column(String(100), nullable=True, index=True)
    mode = Column(String(50), nullable=True)
    status = Column(String(50), nullable=True, index=True)
    payment_status = Column(String(50), nullable=True, index=True)
    amount_total = Column(Integer, nullable=True)
    currency = Column(String(10), nullable=True)
    checkout_url = Column(Text, nullable=True)
    success_url = Column(Text, nullable=True)
    cancel_url = Column(Text, nullable=True)
    livemode = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

# Stripe Checkout Session to Upload link model
class StripeCheckoutSessionUpload(Base):
    __tablename__ = "stripe_checkout_session_uploads"
    __table_args__ = (
        UniqueConstraint("checkout_session_id", "upload_id", name="stripe_checkout_session_uploads_session_upload_key"),
    )

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    checkout_session_id = Column(PGUUID(as_uuid=True), ForeignKey("stripe_checkout_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    upload_id = Column(PGUUID(as_uuid=True), ForeignKey("uploads.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

# Stripe payment audit/link model
class StripePayment(Base):
    __tablename__ = "stripe_payments"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    stripe_payment_intent_id = Column(Text, nullable=True, unique=True, index=True)
    stripe_checkout_session_id = Column(Text, nullable=True, index=True)
    stripe_invoice_id = Column(Text, nullable=True, index=True)
    client_email = Column(Text, nullable=True, index=True)
    upload_id = Column(PGUUID(as_uuid=True), ForeignKey("uploads.id"), nullable=True, index=True)
    status = Column(String(50), nullable=True, index=True)
    amount = Column(Integer, nullable=True)
    amount_received = Column(Integer, nullable=True)
    amount_refunded = Column(Integer, nullable=True)
    currency = Column(String(10), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

# Manual/admin billing override audit model
class BillingOverride(Base):
    __tablename__ = "billing_overrides"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    target_type = Column(String(100), nullable=True, index=True)
    target_id = Column(Text, nullable=True, index=True)
    client_email = Column(Text, nullable=True, index=True)
    override_paid = Column(Boolean, nullable=True)
    reason = Column(Text, nullable=True)
    admin_user_id = Column(Text, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

# Upload file audit model
class UploadFile(Base):
    __tablename__ = "upload_files"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    upload_id = Column(PGUUID(as_uuid=True), ForeignKey("uploads.id"), nullable=True)
    user_email = Column(Text, nullable=False)
    tool_name = Column(Text, nullable=False)
    original_filename = Column(Text, nullable=False)
    content_type = Column(Text, nullable=True)
    byte_size = Column(BigInteger, nullable=True)
    bucket = Column(Text, nullable=False)
    object_path = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

# Durable admin Document Analysis job model
class AdminAnalysisJob(Base):
    __tablename__ = "admin_analysis_jobs"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    status = Column(String(50), nullable=False, server_default=text("'queued'"), index=True)
    created_by_admin_user_id = Column(Text, nullable=True)
    client_email = Column(Text, nullable=True, index=True)
    first_name = Column(Text, nullable=True)
    last_name = Column(Text, nullable=True)
    office_name = Column(Text, nullable=True)
    org_type = Column(String(50), nullable=True)
    phone = Column(Text, nullable=True)
    ghl_cid = Column(Text, nullable=True)
    client_mode = Column(String(50), nullable=True)
    analysis_run_id = Column(Text, nullable=True, unique=True, index=True)
    submission_id = Column(PGUUID(as_uuid=True), ForeignKey("client_submissions.id"), nullable=True)
    progress_percent = Column(Integer, nullable=False, server_default=text("0"))
    current_step = Column(Text, nullable=True)
    error_code = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    canceled_at = Column(DateTime(timezone=True), nullable=True)
    errored_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

# Durable admin Document Analysis job file model
class AdminAnalysisJobFile(Base):
    __tablename__ = "admin_analysis_job_files"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    job_id = Column(PGUUID(as_uuid=True), ForeignKey("admin_analysis_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    tool_name = Column(Text, nullable=False)
    original_filename = Column(Text, nullable=True)
    content_type = Column(Text, nullable=True)
    byte_size = Column(BigInteger, nullable=True)
    upload_file_id = Column(PGUUID(as_uuid=True), ForeignKey("upload_files.id"), nullable=True)
    upload_id = Column(PGUUID(as_uuid=True), ForeignKey("uploads.id"), nullable=True)
    status = Column(String(50), nullable=False, server_default=text("'queued'"), index=True)
    error_code = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    analysis_data = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    errored_at = Column(DateTime(timezone=True), nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)

# Admin Document Analysis PHI/HIPAA processing acknowledgment audit model
class AdminAnalysisPhiAcknowledgment(Base):
    __tablename__ = "admin_analysis_phi_acknowledgments"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    job_id = Column(PGUUID(as_uuid=True), ForeignKey("admin_analysis_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    job_file_id = Column(PGUUID(as_uuid=True), ForeignKey("admin_analysis_job_files.id", ondelete="SET NULL"), nullable=True, index=True)
    tool_name = Column(Text, nullable=True)
    admin_user_id = Column(Text, nullable=True, index=True)
    admin_email = Column(Text, nullable=True)
    initials = Column(Text, nullable=False)
    confirmed_no_phi = Column(Boolean, nullable=False)
    acknowledgment_text = Column(Text, nullable=False)
    acknowledgment_version = Column(String(100), nullable=False)
    ip_address = Column(Text, nullable=True)
    user_agent = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True)

# Admin access mapping (Supabase Auth)
class AdminUser(Base):
    __tablename__ = "admin_users"

    user_id = Column(PGUUID(as_uuid=True), primary_key=True)
    role = Column(String(50), nullable=False)
    email = Column(Text, nullable=True)
    display_name = Column(Text, nullable=True)
    status = Column(Text, nullable=False, server_default=text("'active'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    created_by = Column(PGUUID(as_uuid=True), nullable=True)
    deactivated_at = Column(DateTime(timezone=True), nullable=True)

# Upload portal request model
class UploadPortalRequest(Base):
    __tablename__ = "upload_portal_requests"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    requester_email = Column(Text, nullable=False, index=True)
    token_hash = Column(Text, nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    used_at = Column(DateTime(timezone=True), nullable=True)
    request_ip = Column(Text, nullable=True)

# Upload portal session model
class UploadPortalSession(Base):
    __tablename__ = "upload_portal_sessions"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    request_id = Column(PGUUID(as_uuid=True), ForeignKey("upload_portal_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(Text, nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

# Upload portal file model
class UploadPortalFile(Base):
    __tablename__ = "upload_portal_files"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    request_id = Column(PGUUID(as_uuid=True), ForeignKey("upload_portal_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(PGUUID(as_uuid=True), ForeignKey("upload_portal_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    user_email = Column(Text, nullable=True, index=True)
    gcs_bucket = Column(Text, nullable=False)
    object_name = Column(Text, nullable=False)
    original_filename = Column(Text, nullable=False)
    content_type = Column(Text, nullable=True)
    byte_size = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

# Function to get uploads from the DB
def get_uploads(db):
    return db.query(Upload).all()

# Admin model
class Admin(Base):
    __tablename__ = "admins"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(255), unique=True, index=True)
    password_hash = Column(String(255))
    email = Column(String(255))
    must_change_password = Column(Boolean, default=False)

# Function to hash a password
def hash_password(password: str) -> str:
    """Hash a password using bcrypt"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

# Function to verify a password
def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

# Function to get admin by username
def get_admin_by_username(db, username: str):
    """Retrieve an admin by username"""
    return db.query(Admin).filter(Admin.username == username).first()

# Function to create an admin
def create_admin(db, username: str, password: str, email: str = "", must_change_password: bool = False):
    """Create a new admin user with hashed password"""
    hashed_pw = hash_password(password)
    admin = Admin(username=username, password_hash=hashed_pw, email=email, must_change_password=must_change_password)
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin

# Function to delete a user and their uploads
def delete_user(db, user_email: str):
    """Delete a user and all their uploads"""
    db.query(ClientSubmission).filter(ClientSubmission.user_email == user_email).delete()
    db.query(Upload).filter(Upload.user_email == user_email).delete()
    db.query(User).filter(User.email == user_email).delete()
    db.commit()

# Function to get uploads by user email
def get_uploads_by_email(db, email: str):
    """Get all uploads for a specific user"""
    return db.query(Upload).filter(Upload.user_email == email).all()

# Function to update admin password
def update_admin_password(db, username: str, new_password: str, must_change: bool = False):
    """Update an admin's password"""
    admin = get_admin_by_username(db, username)
    if admin:
        admin.password_hash = hash_password(new_password)
        admin.must_change_password = must_change
        db.commit()
        return True
    return False

# Function to get all admins
def get_all_admins(db):
    """Get all admin users"""
    return db.query(Admin).all()

# Function to delete an admin
def delete_admin(db, username: str, current_admin_username: str):
    """
    Delete an admin user with safeguards:
    - Cannot delete self (current logged-in admin)
    - Cannot delete the last remaining admin
    Returns: (success: bool, message: str)
    """
    if username == current_admin_username:
        return False, "Cannot delete your own admin account"
    
    admin_count = db.query(Admin).count()
    if admin_count <= 1:
        return False, "Cannot delete the last remaining admin"
    
    admin = get_admin_by_username(db, username)
    if not admin:
        return False, "Admin not found"
    
    db.delete(admin)
    db.commit()
    return True, f"Admin '{username}' deleted successfully"
