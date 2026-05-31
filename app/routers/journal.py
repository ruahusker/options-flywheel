from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.journal import TradeJournalEntry
from app.routers.common import templates


router = APIRouter(prefix="/journal", tags=["journal"])


@router.get("")
def journal_page(request: Request, db: Session = Depends(get_db)):
    entries = db.execute(select(TradeJournalEntry).order_by(desc(TradeJournalEntry.created_at))).scalars().all()
    return templates.TemplateResponse(request, "journal.html", {"entries": entries})


@router.post("")
def add_journal_entry(
    request: Request,
    ticker: str = Form(...),
    strategy: str = Form(""),
    action: str = Form(...),
    contracts: int = Form(0),
    strike: float | None = Form(None),
    expiration: str = Form(""),
    credit_debit: float | None = Form(None),
    sata_contribution: float | None = Form(None),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    entry = TradeJournalEntry(
        ticker=ticker.upper(),
        strategy=strategy,
        action=action,
        contracts=contracts,
        strike=strike,
        expiration=date.fromisoformat(expiration) if expiration else None,
        credit_debit=credit_debit,
        sata_contribution=sata_contribution,
        notes=notes,
    )
    db.add(entry)
    db.commit()
    entries = db.execute(select(TradeJournalEntry).order_by(desc(TradeJournalEntry.created_at))).scalars().all()
    return templates.TemplateResponse(request, "journal.html", {"entries": entries, "message": "Journal entry added."})
