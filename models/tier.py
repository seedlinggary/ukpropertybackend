import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from models import Base

# Canonical feature keys — super-admin can assign any of these to any tier.
# All keys known to the system; new ones can be added by super-admin as free-text too.
KNOWN_FEATURES = [
    "properties_search",
    "ownership_search",
    "intelligence_profile",
    "planning_search",
    "pins_appeals",
    "postcode_analysis",
    "calculators",
    "scraper_view",
    "article4_editor",
    "ai_search",
    "export_data",
]

# Admin permissions that can be granted to admin-role users
ADMIN_PERMISSION_KEYS = [
    "view_login_stats",
    "manage_users",
    "manage_tiers",
    "manage_permissions",
]


class Tier(Base):
    __tablename__ = "tiers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(50), unique=True, nullable=False)          # slug: free | pro | enterprise
    display_name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    price_monthly_cents = Column(Integer, nullable=False, default=0)
    price_yearly_cents = Column(Integer, nullable=False, default=0)
    stripe_price_id_monthly = Column(String(255), nullable=True)
    stripe_price_id_yearly = Column(String(255), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "price_monthly_cents": self.price_monthly_cents,
            "price_yearly_cents": self.price_yearly_cents,
            "stripe_price_id_monthly": self.stripe_price_id_monthly,
            "stripe_price_id_yearly": self.stripe_price_id_yearly,
            "is_active": self.is_active,
            "sort_order": self.sort_order,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TierFeature(Base):
    __tablename__ = "tier_features"
    __table_args__ = (UniqueConstraint("tier_id", "feature_key", name="uq_tier_feature"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tier_id = Column(UUID(as_uuid=True), ForeignKey("tiers.id", ondelete="CASCADE"), nullable=False, index=True)
    feature_key = Column(String(100), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    limit_value = Column(Integer, nullable=True)      # None = unlimited
    unit = Column(String(50), nullable=True)          # per_day | per_month | total
    description = Column(Text, nullable=True)

    def to_dict(self):
        return {
            "id": str(self.id),
            "tier_id": str(self.tier_id),
            "feature_key": self.feature_key,
            "enabled": self.enabled,
            "limit_value": self.limit_value,
            "unit": self.unit,
            "description": self.description,
        }


class AdminPermission(Base):
    __tablename__ = "admin_permissions"
    __table_args__ = (UniqueConstraint("user_id", "permission_key", name="uq_admin_permission"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    permission_key = Column(String(100), nullable=False)
    granted_by_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    granted_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "permission_key": self.permission_key,
            "granted_by_id": str(self.granted_by_id) if self.granted_by_id else None,
            "granted_at": self.granted_at.isoformat() if self.granted_at else None,
        }
