from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class TradeJournalEntrySchema(BaseModel):
    ticker: str
    strategy: str | None = None
    action: str
    contracts: int | None = None
    strike: float | None = None
    expiration: date | None = None
    credit_debit: float | None = None
    assignment_result: str | None = None
    roll_result: str | None = None
    sata_contribution: float | None = None
    notes: str | None = None
    realized_option_pnl: float | None = None
    foregone_upside: float | None = None
    buy_hold_comparison: float | None = None
