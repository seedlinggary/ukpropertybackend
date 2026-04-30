import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Float, Text, Boolean, DateTime, JSON
from sqlalchemy.dialects.postgresql import UUID
from models import Base


class PropertyListing(Base):
    __tablename__ = "property_listings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source = Column(String(50), nullable=False)
    listing_url = Column(String(1000), unique=True, nullable=False, index=True)
    city = Column(String(100), index=True)
    address = Column(String(500))
    price = Column(Integer)
    bedrooms = Column(Integer)
    bathrooms = Column(Integer)
    size_m2 = Column(Float)
    property_type = Column(String(100))
    description = Column(Text)
    agent_name = Column(String(200))
    agent_phone = Column(String(50))
    image_url = Column(String(1000))
    lat = Column(Float)
    lng = Column(Float)
    article4 = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScraperRun(Base):
    __tablename__ = "scraper_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source = Column(String(50))
    cities = Column(JSON)
    status = Column(String(20), default="running")  # running | completed | failed
    listings_added = Column(Integer, default=0)
    listings_seen = Column(Integer, default=0)
    error = Column(Text)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)
