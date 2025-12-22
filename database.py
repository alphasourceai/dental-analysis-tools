import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Database connection string using environment variable
DATABASE_URL = os.getenv("DATABASE_URL")

# Create a database engine with Neon serverless optimizations
# pool_pre_ping: Test connections before use to catch stale connections
# pool_recycle: Recycle connections after 300 seconds (5 min) to prevent timeout issues
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300
)

# Base class for models
Base = declarative_base()

# Create a sessionmaker instance to interact with the DB
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Function to get a database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
