from __future__ import annotations

from sqlalchemy import Boolean, Column, Float, Integer, String, Text

from app.database import Base


class SATASettings(Base):
    __tablename__ = "sata_settings"

    id = Column(Integer, primary_key=True)
    annual_dividend_rate = Column(Float, default=0.13, nullable=False)
    # SATA pays dividends daily, so daily is the realistic compounding/DRIP cadence.
    compounding_mode = Column(String(30), default="daily", nullable=False)
    drip_enabled = Column(Boolean, default=True, nullable=False)
    business_day_payments = Column(Boolean, default=False, nullable=False)
    assumed_price = Column(Float, default=100.0, nullable=False)
    # Effective tax on distributions (0.0 in a tax-advantaged account; preferred dividends are taxable otherwise).
    tax_rate = Column(Float, default=0.0, nullable=False)
    notes = Column(Text)
