import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from models import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=True)
    password_hash = Column(String(255), nullable=True)   # null for Google-only accounts
    google_id = Column(String(255), unique=True, nullable=True, index=True)
    avatar_url = Column(String(1000), nullable=True)
    role = Column(String(50), nullable=False, default="user")  # user | admin | super_admin
    tier_id = Column(UUID(as_uuid=True), ForeignKey("tiers.id", ondelete="SET NULL"), nullable=True)
    stripe_customer_id = Column(String(255), nullable=True, unique=True)
    stripe_subscription_id = Column(String(255), nullable=True)
    subscription_status = Column(String(50), nullable=True)  # active|trialing|past_due|canceled
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)

    def to_dict(self, include_sensitive: bool = False):
        d = {
            "id": str(self.id),
            "email": self.email,
            "name": self.name,
            "avatar_url": self.avatar_url,
            "role": self.role,
            "tier_id": str(self.tier_id) if self.tier_id else None,
            "subscription_status": self.subscription_status,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }
        if include_sensitive:
            d["stripe_customer_id"] = self.stripe_customer_id
            d["stripe_subscription_id"] = self.stripe_subscription_id
        return d


class LoginEvent(Base):
    __tablename__ = "login_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ip_address = Column(String(50), nullable=True)
    user_agent = Column(Text, nullable=True)
    method = Column(String(20), nullable=True)   # 'password' | 'google'
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
