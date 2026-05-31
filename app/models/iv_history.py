from __future__ import annotations

from sqlalchemy import Column, Date, Float, Integer, String, UniqueConstraint

from app.database import Base


class IVHistory(Base):
    """One at-the-money implied-volatility observation per symbol per day.

    Built incrementally on each live option-chain pull so that IV rank / percentile can be
    computed (current IV relative to its own trailing range), which is far more informative
    for premium selling than the raw absolute IV level.
    """

    __tablename__ = "iv_history"
    __table_args__ = (UniqueConstraint("symbol", "observed_on", name="uq_iv_history_symbol_date"),)

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False, index=True)
    observed_on = Column(Date, nullable=False)
    atm_iv = Column(Float, nullable=False)
