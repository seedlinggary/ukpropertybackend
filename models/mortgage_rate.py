import uuid
from datetime import datetime

from sqlalchemy import Column, String, Integer, Boolean, Numeric, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID

from models import Base


class MortgageRate(Base):
    __tablename__ = "mortgage_rates"

    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lender         = Column(String(200), nullable=False)
    rate           = Column(Numeric(6, 3), nullable=False)          # annual %, e.g. 4.350
    product_fee    = Column(Integer,       nullable=False, default=0)  # £
    true_cost      = Column(String(500),   nullable=False, default="")
    example        = Column(Text,          nullable=False, default="")
    product_type   = Column(String(100),   nullable=False)           # "2-year fixed", etc.
    ltv_tier       = Column(Integer,       nullable=False)            # max LTV %, e.g. 75 or 90
    repayment_type = Column(String(20),    nullable=False, default="both")
    # "repayment" | "interest_only" | "both"
    is_active      = Column(Boolean,       nullable=False, default=True)
    sort_order     = Column(Integer,       nullable=False, default=0)
    updated_at     = Column(DateTime,      nullable=False, default=datetime.utcnow,
                            onupdate=datetime.utcnow)
    created_at     = Column(DateTime,      nullable=False, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":             str(self.id),
            "lender":         self.lender,
            "rate":           float(self.rate),
            "product_fee":    self.product_fee,
            "true_cost":      self.true_cost,
            "example":        self.example,
            "product_type":   self.product_type,
            "ltv_tier":       self.ltv_tier,
            "repayment_type": self.repayment_type,
            "is_active":      self.is_active,
            "sort_order":     self.sort_order,
            "updated_at":     self.updated_at.isoformat() if self.updated_at else None,
        }
