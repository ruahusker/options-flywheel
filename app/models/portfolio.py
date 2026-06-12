from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base

POSITIONS_KIND = "fidelity_positions"
HISTORY_KIND = "fidelity_history"


def is_position_snapshot(snapshot) -> bool:
    """True for snapshots from a positions export (real marked prices). History imports
    aggregate transactions and carry stale last-trade prices, so they are kept for the journal
    but must not drive NAV/dashboard numbers. New imports store the kind in notes; older rows
    are classified by filename."""
    notes = snapshot.notes or ""
    if notes in (POSITIONS_KIND, HISTORY_KIND):
        return notes == POSITIONS_KIND
    return "history" not in (snapshot.source_filename or "").lower()


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    source_filename = Column(String(255))
    account_number = Column(String(100))
    account_name = Column(String(255))
    tax_status = Column(String(50), default="tax_free", nullable=False)
    total_value = Column(Float)
    notes = Column(Text)

    holdings = relationship("Holding", cascade="all, delete-orphan", back_populates="snapshot")
    option_positions = relationship("OptionPosition", cascade="all, delete-orphan", back_populates="snapshot")
    cash_positions = relationship("CashPosition", cascade="all, delete-orphan", back_populates="snapshot")


class Holding(Base):
    __tablename__ = "holdings"

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, ForeignKey("portfolio_snapshots.id"), nullable=False, index=True)
    account_number = Column(String(100))
    account_name = Column(String(255))
    symbol = Column(String(64), nullable=False, index=True)
    description = Column(Text)
    quantity = Column(Float)
    last_price = Column(Float)
    current_value = Column(Float)
    cost_basis_total = Column(Float)
    average_cost_basis = Column(Float)
    percent_of_account = Column(Float)
    asset_class = Column(String(50), default="unknown", nullable=False)
    position_type = Column(String(50))

    snapshot = relationship("PortfolioSnapshot", back_populates="holdings")


class OptionPosition(Base):
    __tablename__ = "option_positions"

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, ForeignKey("portfolio_snapshots.id"), nullable=False, index=True)
    account_number = Column(String(100))
    account_name = Column(String(255))
    raw_symbol = Column(String(128), nullable=False)
    normalized_symbol = Column(String(128), nullable=False, index=True)
    underlying = Column(String(32), nullable=False, index=True)
    expiration = Column(Date, nullable=False)
    option_type = Column(String(10), nullable=False)
    strike = Column(Float, nullable=False)
    side = Column(String(10), nullable=False)
    contracts = Column(Integer, nullable=False)
    quantity = Column(Float, nullable=False)
    last_price = Column(Float)
    current_value = Column(Float)
    average_cost_basis = Column(Float)
    description = Column(Text)

    snapshot = relationship("PortfolioSnapshot", back_populates="option_positions")


class CashPosition(Base):
    __tablename__ = "cash_positions"

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, ForeignKey("portfolio_snapshots.id"), nullable=False, index=True)
    account_number = Column(String(100))
    account_name = Column(String(255))
    symbol = Column(String(64), nullable=False)
    description = Column(Text)
    current_value = Column(Float)

    snapshot = relationship("PortfolioSnapshot", back_populates="cash_positions")
