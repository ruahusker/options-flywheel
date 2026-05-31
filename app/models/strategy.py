from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    portfolio_snapshot_id = Column(Integer, ForeignKey("portfolio_snapshots.id"), nullable=True, index=True)
    market_data_timestamp = Column(DateTime)
    strategy_name = Column(String(100), nullable=False)
    optioned_pct = Column(Float, nullable=False)
    untouched_pct = Column(Float, nullable=False)
    call_delta_min = Column(Float)
    call_delta_max = Column(Float)
    put_delta_min = Column(Float)
    put_delta_max = Column(Float)
    dte_min = Column(Integer)
    dte_max = Column(Integer)
    sata_rate = Column(Float)
    tax_status = Column(String(50), default="tax_free")
    notes = Column(Text)

    candidates = relationship("StrategyCandidate", cascade="all, delete-orphan", back_populates="strategy_run")
    recommendations = relationship("Recommendation", cascade="all, delete-orphan", back_populates="strategy_run")


class StrategyCandidate(Base):
    __tablename__ = "strategy_candidates"

    id = Column(Integer, primary_key=True)
    strategy_run_id = Column(Integer, ForeignKey("strategy_runs.id"), nullable=False, index=True)
    symbol = Column(String(32), nullable=False)
    action = Column(String(50), nullable=False)
    contracts = Column(Integer, nullable=False)
    expiration = Column(Date)
    strike = Column(Float)
    option_type = Column(String(10))
    side = Column(String(10))
    delta = Column(Float)
    bid = Column(Float)
    ask = Column(Float)
    mid = Column(Float)
    expected_credit = Column(Float)
    collateral_required = Column(Float)
    premium_yield_weekly = Column(Float)
    premium_yield_annualized = Column(Float)
    assignment_probability_proxy = Column(Float)
    upside_cap = Column(Float)
    upside_preserved_score = Column(Float)
    liquidity_score = Column(Float)
    trend_alignment_score = Column(Float)
    iv_score = Column(Float)
    scenario_score = Column(Float)
    total_score = Column(Float)
    warnings_json = Column(Text)

    strategy_run = relationship("StrategyRun", back_populates="candidates")


class Recommendation(Base):
    __tablename__ = "recommendations"

    id = Column(Integer, primary_key=True)
    strategy_run_id = Column(Integer, ForeignKey("strategy_runs.id"), nullable=False, index=True)
    symbol = Column(String(32), nullable=False)
    recommended_action = Column(String(50), nullable=False)
    contracts = Column(Integer)
    expiration = Column(Date)
    strike = Column(Float)
    option_type = Column(String(10))
    delta = Column(Float)
    expected_credit = Column(Float)
    reason = Column(Text)
    warnings = Column(Text)
    scenario_summary_json = Column(Text)

    strategy_run = relationship("StrategyRun", back_populates="recommendations")
