import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB

from models import Base

# Default importance levels for each preference dimension
DEFAULT_IMPORTANCE = {
    "price":         "required",   # required | high | medium | low | ignore
    "bedrooms":      "high",
    "location":      "high",
    "article4":      "medium",
    "property_type": "medium",
    "tenure":        "low",
    "price_per_m2":  "medium",
    "size":          "low",
    "source":        "low",
}

# Default prefs JSON shape — every field the UI can set
DEFAULT_PREFS = {
    # Location
    "preferred_cities":   [],       # ["London", "Manchester"]
    "center_lat":         None,
    "center_lng":         None,
    "center_label":       None,
    "radius_km":          5.0,

    # Price / finance
    "max_price":          None,
    "min_price":          None,
    "deposit_amount":     None,     # £ deposit (informational — helps derive max price)
    "target_ltv":         75,       # % LTV target (informational)

    # Property basics
    "min_bedrooms":       None,
    "property_types":     [],       # ["flat"] | ["house"] | [] = any
    "tenure":             "",       # "" | "freehold" | "leasehold" | "share_of_freehold"
    "article4":           "any",    # "yes" | "no" | "any"
    "source":             "",       # "" | "zoopla" | "auction_houses"

    # Size / value metrics
    "min_size_m2":        None,
    "max_size_m2":        None,
    "max_price_per_m2":   None,

    # Strategy (informational label, doesn't affect scoring)
    "strategies":         [],       # ["btl", "flip", "development", "live_in"]

    # Recency
    "max_days_old":       None,     # only show listings added within N days

    # Email
    "email_notifications":    False,
    "notification_frequency": "daily",   # "daily" | "weekly"

    # Importance weights per dimension
    "importance": DEFAULT_IMPORTANCE,

    # Calculator defaults — pre-fill calculator inputs with the user's
    # typical assumptions rather than generic hardcoded values.
    "calculator_defaults": {
        "sdlt":     {"region": "england", "buyer_type": "additional", "use_type": "residential", "non_uk_resident": False},
        "bridging": {"ltv_pct": 70, "monthly_rate": 0.75, "term_months": 12, "arrange_fee": 2, "exit_fee": 1, "interest_style": "rolledup"},
        "deal":     {"is_additional_dwelling": True, "finance_type": "mortgage", "mortgage_ltv": 75, "mortgage_rate": 5.0, "vacancy_pct": 5, "management_pct": 10, "maintenance_pct": 10, "cap_growth_pct": 3},
        "avm":      {"avm_type": "standard"},
        "roi":      {"hold_years": 5, "annual_growth": 3, "discount_rate": 8},
        "tax":      {"salary": 12570, "period": "yearly"},
        "mortgage": {"ltv": 75, "repayment_type": "repayment"},
    },
}


class UserPropertyPrefs(Base):
    __tablename__ = "user_property_prefs"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_property_prefs"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    prefs_json = Column(JSONB, nullable=False, default=lambda: dict(DEFAULT_PREFS))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        import copy
        base = copy.deepcopy(DEFAULT_PREFS)
        if self.prefs_json:
            _deep_merge(base, self.prefs_json)
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "prefs": base,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
