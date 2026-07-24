import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("SUPABASE_DB_URL")

engine = create_engine(
    DATABASE_URL,
    # use_native_hstore=False must be passed here so it's set on the dialect
    # instance BEFORE dialect.on_connect() builds its closure.  Setting it
    # after create_engine() is too late — the closure already includes hstore
    # detection, and Supabase drops SSL connections during that catalog query.
    use_native_hstore=False,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_recycle=300,
    connect_args={"connect_timeout": 30},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
