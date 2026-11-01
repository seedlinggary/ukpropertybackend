import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from models import Base


class UserDismissedProperty(Base):
    __tablename__ = "user_dismissed_properties"
    __table_args__ = (
        UniqueConstraint("user_id", "property_id", name="uq_dismissed_property"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    property_id = Column(UUID(as_uuid=True), nullable=False)
    dismissed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
