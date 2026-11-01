import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Index
from sqlalchemy.dialects.postgresql import UUID
from models import Base


class FeatureUsage(Base):
    __tablename__ = "feature_usage"

    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id    = Column(UUID(as_uuid=True), nullable=True, index=True)   # null = unauthenticated
    feature_key = Column(String(100), nullable=False)
    action     = Column(String(100), nullable=True)   # e.g. "search", "view", "export"
    ip_address = Column(String(50), nullable=True)
    created_at  = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_fu_feature_created", "feature_key", "created_at"),
        Index("ix_fu_user_feature",    "user_id",     "feature_key"),
    )
