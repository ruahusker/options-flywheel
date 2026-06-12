from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.journal import TradeJournalEntry
from app.routers.uploads import _import_history_journal_entries
from app.services.journal_dedupe import collapse_dual_section, is_dual_section, parse_source


@dataclass
class FakeEntry:
    created_at: datetime
    credit_debit: float
    contracts: int = 1
    account_number: str = "111"
    account_name: str = "Test IRA"
    ticker: str = "IBIT"
    strategy: str = "covered call"
    action: str = "YOU SOLD OPENING TRANSACTION CALL (IBIT)"
    strike: float = 40.0
    expiration: date = field(default_factory=lambda: date(2026, 6, 12))
    sata_contribution: float | None = None
    notes: str = ""


LINE = lambda e: e.created_at  # noqa: E731 - entries here are ordered by created_at


def test_dual_section_file_is_halved_with_penny_tolerance():
    day = datetime(2026, 6, 9)
    # Combined section + per-account section: same two trades, one re-rounded by a cent.
    entries = [
        FakeEntry(day, 693.21, contracts=16),
        FakeEntry(day, 373.29, contracts=7),
        FakeEntry(day, 693.21, contracts=16),
        FakeEntry(day, 373.28, contracts=7),
    ]
    assert is_dual_section(entries, sort_key=LINE)
    collapsed = collapse_dual_section(entries, sort_key=LINE)
    assert sorted(e.contracts for e in collapsed) == [7, 16]


def test_normal_file_with_repeated_fills_is_untouched():
    day = datetime(2026, 5, 29)
    # A 34-contract order printing as 1+1+1+31 fills, plus unrelated rows: genuine, keep all.
    entries = [
        FakeEntry(day, 25.33),
        FakeEntry(day, 25.33),
        FakeEntry(day, 25.33),
        FakeEntry(day, 785.12, contracts=31),
        FakeEntry(day, 405.21, contracts=19, strike=20.5, ticker="ASST"),
        FakeEntry(day, 151.96, contracts=6, account_number="222"),
        FakeEntry(day, 354.58, contracts=14, account_number="333"),
        FakeEntry(day, 26.33, account_number="333"),
    ]
    assert not is_dual_section(entries, sort_key=LINE)
    assert len(collapse_dual_section(entries, sort_key=LINE)) == len(entries)


def test_dual_section_keeps_half_of_real_double_fills():
    day = datetime(2026, 6, 2)
    # Two real 2-lot fills, listed in both sections -> 4 copies, keep 2.
    entries = [FakeEntry(day, -14.04, contracts=2, action="YOU BOUGHT CLOSING TRANSACTION CALL (IBIT)")] * 2 + [
        FakeEntry(day, -14.05, contracts=2, action="YOU BOUGHT CLOSING TRANSACTION CALL (IBIT)")
    ] * 2
    collapsed = collapse_dual_section(entries, sort_key=LINE)
    assert len(collapsed) == 2


def test_import_skips_penny_different_re_download():
    session = make_session()
    first = [FakeEntry(datetime(2026, 6, 5), 373.29, contracts=7, notes="Imported from a.csv line 9")]
    assert _import_history_journal_entries(session, first) == 1
    session.commit()

    # Overlapping re-download of the same trade, rounded a cent differently.
    again = [FakeEntry(datetime(2026, 6, 5), 373.28, contracts=7, notes="Imported from b.csv line 57")]
    assert _import_history_journal_entries(session, again) == 0
    assert session.query(TradeJournalEntry).count() == 1


def test_import_keeps_distinct_same_size_trade_at_different_price():
    session = make_session()
    assert _import_history_journal_entries(session, [FakeEntry(datetime(2026, 6, 5), 52.33)]) == 1
    session.commit()
    # Same day/size/strike but a different order at a different price: a real second trade.
    assert _import_history_journal_entries(session, [FakeEntry(datetime(2026, 6, 5), 59.33)]) == 1
    session.commit()
    assert session.query(TradeJournalEntry).count() == 2


def test_parse_source():
    assert parse_source("desc; 111; Imported from Accounts_History (10).csv line 74") == (
        "Accounts_History (10).csv",
        74,
    )
    assert parse_source("manual entry") is None
    assert parse_source(None) is None


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, future=True)()
