from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, Date, DateTime, Float, Integer, String, Text

from app.database import Base


class TradeJournalEntry(Base):
    __tablename__ = "trade_journal_entries"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    account_number = Column(String(100))
    account_name = Column(String(255))
    ticker = Column(String(32), nullable=False, index=True)
    strategy = Column(String(100))
    action = Column(String(50), nullable=False)
    contracts = Column(Integer)
    strike = Column(Float)
    expiration = Column(Date)
    credit_debit = Column(Float)
    assignment_result = Column(String(100))
    roll_result = Column(String(100))
    sata_contribution = Column(Float)
    notes = Column(Text)
    realized_option_pnl = Column(Float)
    foregone_upside = Column(Float)
    buy_hold_comparison = Column(Float)
