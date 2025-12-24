import os
import bcrypt
from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, Text, text
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

# Client submission snapshot model
class ClientSubmission(Base):
    __tablename__ = "client_submissions"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    user_email = Column(Text, nullable=False, index=True)
    first_name = Column(String(255))
    last_name = Column(String(255))
    office_name = Column(String(255))
    org_type = Column(String(50))
    submitted_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"), index=True)

# Function to get users from the DB
def get_users(db):
    return db.query(User).all()

# Upload model
class Upload(Base):
    __tablename__ = "uploads"

    id = Column(Integer, primary_key=True, index=True)
    file_name = Column(String(255))
    tool_name = Column(String(100))
    upload_time = Column(String(100))
    user_email = Column(String(255), index=True)
    analysis_data = Column(Text)
    submission_id = Column(PGUUID(as_uuid=True), nullable=True, index=True)

# Upload file audit model
class UploadFile(Base):
    __tablename__ = "upload_files"

    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))
    upload_id = Column(PGUUID(as_uuid=True), nullable=True)
    user_email = Column(Text, nullable=False)
    tool_name = Column(Text, nullable=False)
    original_filename = Column(Text, nullable=False)
    content_type = Column(Text, nullable=True)
    byte_size = Column(BigInteger, nullable=True)
    bucket = Column(Text, nullable=False)
    storage_path = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

# Admin access mapping (Supabase Auth)
class AdminUser(Base):
    __tablename__ = "admin_users"

    user_id = Column(PGUUID(as_uuid=True), primary_key=True)
    role = Column(String(50), nullable=False)

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
