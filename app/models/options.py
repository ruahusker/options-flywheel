from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.database import Base


class OptionChainSnapshot(Base):
    __tablename__ = "option_chain_snapshots"

    id = Column(Integer, primary_key=True)
    provider = Column(String(50), nullable=False)
    underlying = Column(String(32), nullable=False, index=True)
    expiration = Column(Date, nullable=False, index=True)
    fetched_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_stale = Column(Boolean, default=False, nullable=False)
    market_status = Column(String(50), default="unknown", nullable=False)

    contracts = relationship("OptionContract", cascade="all, delete-orphan", back_populates="chain_snapshot")


class OptionContract(Base):
    __tablename__ = "option_contracts"

    id = Column(Integer, primary_key=True)
    chain_snapshot_id = Column(Integer, ForeignKey("option_chain_snapshots.id"), nullable=False, index=True)
    underlying = Column(String(32), nullable=False, index=True)
    expiration = Column(Date, nullable=False, index=True)
    option_type = Column(String(10), nullable=False)
    strike = Column(Float, nullable=False)
    bid = Column(Float)
    ask = Column(Float)
    mid = Column(Float)
    last = Column(Float)
    volume = Column(Integer)
    open_interest = Column(Integer)
    implied_volatility = Column(Float)
    delta = Column(Float)
    gamma = Column(Float)
    theta = Column(Float)
    vega = Column(Float)
    dte = Column(Integer)
    provider_symbol = Column(String(128))
    liquidity_score = Column(Float)

    chain_snapshot = relationship("OptionChainSnapshot", back_populates="contracts")
