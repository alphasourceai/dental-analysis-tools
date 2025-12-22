from database import engine, Base
from models import User, Upload, ClientSubmission

# Create all tables in the database (if they don't exist)
print("Creating database tables...")
Base.metadata.create_all(bind=engine)
print("Database tables created successfully!")
