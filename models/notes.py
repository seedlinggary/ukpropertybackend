import uuid
from datetime import datetime

from sqlalchemy import Column, String, Text, DateTime, ForeignKey, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB

from models import Base


class UserNote(Base):
    __tablename__ = "user_notes"
    __table_args__ = (
        Index("ix_user_notes_user_id", "user_id"),
        Index("ix_user_notes_property_id", "property_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # "general" or "property"
    note_type = Column(String(20), nullable=False, default="general")
    property_id = Column(String(255), nullable=True)
    property_address = Column(String(500), nullable=True)
    title = Column(String(255), nullable=True)
    content = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "note_type": self.note_type,
            "property_id": self.property_id,
            "property_address": self.property_address,
            "title": self.title,
            "content": self.content,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class UserFavorite(Base):
    __tablename__ = "user_favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "property_id", name="uq_user_favorite"),
        Index("ix_user_favorites_user_id", "user_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    property_id = Column(String(255), nullable=False)
    property_address = Column(String(500), nullable=True)
    property_snapshot = Column(JSONB, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "property_id": self.property_id,
            "property_address": self.property_address,
            "property_snapshot": self.property_snapshot,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
