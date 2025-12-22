"""
Setup script to initialize the admins table and create initial admin users.
Run this script once to set up the admin accounts.
"""
import os
import getpass
from database import Base, engine, get_db
from models import Admin, create_admin

def setup_admins():
    """Create the admins table and add initial admin users"""
    print("Creating admins table...")
    Base.metadata.create_all(bind=engine)
    
    # Get database session
    db = next(get_db())
    
    try:
        # Check if admins already exist
        existing_admins = db.query(Admin).all()
        if existing_admins:
            print(f"Found {len(existing_admins)} existing admin(s).")
            for admin in existing_admins:
                print(f"  - {admin.username}")
            response = input("\nDo you want to add more admins? (yes/no): ").strip().lower()
            if response != 'yes':
                print("Setup cancelled.")
                return
        
        # Prompt for admin credentials
        print("\n=== Add Admin User ===")
        while True:
            username = input("Enter admin username (or 'done' to finish): ").strip()
            if username.lower() == 'done':
                break
            
            if not username:
                print("Username cannot be empty.")
                continue
            
            # Check if admin already exists
            existing = db.query(Admin).filter(Admin.username == username).first()
            if existing:
                print(f"Admin '{username}' already exists. Try another username.")
                continue
            
            # Prompt for password securely
            password = getpass.getpass(f"Enter password for '{username}': ")
            if not password:
                print("Password cannot be empty.")
                continue
            
            password_confirm = getpass.getpass("Confirm password: ")
            if password != password_confirm:
                print("Passwords do not match. Try again.")
                continue
            
            # Create admin
            try:
                create_admin(db, username, password)
                print(f"✓ Created admin: {username}")
            except Exception as e:
                print(f"✗ Error creating admin '{username}': {e}")
                db.rollback()
        
        print("\nAdmin setup complete!")
        
        # List all admins
        all_admins = db.query(Admin).all()
        print(f"\nTotal admins: {len(all_admins)}")
        for admin in all_admins:
            print(f"  - {admin.username}")
    
    finally:
        db.close()

if __name__ == "__main__":
    setup_admins()
