from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.journal import TradeJournalEntry
from app.services.trade_ledger import build_trade_rounds, journal_date_span, rounds_in_range, summarize_range


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def add(session, at, action, contracts, strike, expiration, credit, account="Steve-Trad IRA", ticker="IBIT"):
    session.add(
        TradeJournalEntry(
            created_at=at,
            account_name=account,
            ticker=ticker,
            strategy="",
            action=action,
            contracts=contracts,
            strike=strike,
            expiration=expiration,
            credit_debit=credit,
        )
    )


SELL_CALL = "YOU SOLD OPENING TRANSACTION CALL (IBIT) ISHARES BITCOIN"
BUY_CALL = "YOU BOUGHT CLOSING TRANSACTION CALL (IBIT) ISHARES BITCOIN"
SELL_PUT = "YOU SOLD OPENING TRANSACTION PUT (IBIT) ISHARES BITCOIN"
ASSIGNED_CALL = "ASSIGNED as of Jun-08-2026 CALL (IBIT) ISHARES BITCOIN"
BUY_OPEN_CALL = "YOU BOUGHT OPENING TRANSACTION CALL (IBIT) ISHARES BITCOIN"
EXPIRED_CALL = "EXPIRED CALL (IBIT) ISHARES BITCOIN"


def test_bought_back_round_pairs_open_and_close():
    session = make_session()
    add(session, datetime(2026, 6, 1), SELL_CALL, 5, 40.0, date(2026, 6, 5), 500.0)
    add(session, datetime(2026, 6, 4), BUY_CALL, 5, 40.0, date(2026, 6, 5), -100.0)
    session.commit()

    (r,) = build_trade_rounds(session, as_of=datetime(2026, 6, 10))
    assert r.outcome == "bought back"
    assert r.premium_collected == 500.0
    assert r.buyback_cost == 100.0
    assert r.net_pnl == 400.0
    assert r.closed_at == datetime(2026, 6, 4)
    assert r.days_held == 3
    assert r.side == "short"


def test_assigned_round_keeps_full_premium():
    session = make_session()
    add(session, datetime(2026, 6, 1), SELL_CALL, 3, 33.5, date(2026, 6, 8), 300.0)
    add(session, datetime(2026, 6, 9), ASSIGNED_CALL, 3, 33.5, date(2026, 6, 8), 0.0)
    session.commit()

    (r,) = build_trade_rounds(session, as_of=datetime(2026, 6, 10))
    assert r.outcome == "assigned"
    assert r.net_pnl == 300.0


def test_open_round_and_passive_expiry():
    session = make_session()
    add(session, datetime(2026, 6, 9), SELL_PUT, 2, 35.0, date(2026, 6, 12), 100.0)
    session.commit()

    (r,) = build_trade_rounds(session, as_of=datetime(2026, 6, 10))
    assert r.outcome == "open"
    assert r.closed_at is None

    # Past expiration with no explicit close row: it expired worthless, premium kept.
    (r,) = build_trade_rounds(session, as_of=datetime(2026, 6, 15))
    assert r.outcome == "expired"
    assert r.net_pnl == 100.0


def test_long_round_classified_and_separate_groups():
    session = make_session()
    add(session, datetime(2026, 5, 29), BUY_OPEN_CALL, 19, 22.5, date(2026, 6, 5), -179.99)
    add(session, datetime(2026, 6, 8), EXPIRED_CALL, 19, 22.5, date(2026, 6, 5), 0.0)
    add(session, datetime(2026, 6, 1), SELL_CALL, 1, 40.0, date(2026, 6, 5), 25.0, account="Nicole-Trad IRA")
    session.commit()

    rounds = build_trade_rounds(session, as_of=datetime(2026, 6, 10))
    assert len(rounds) == 2
    long_round = next(r for r in rounds if r.side == "long")
    assert long_round.outcome == "expired"
    assert long_round.net_pnl == -179.99


def test_summarize_range_and_annualization():
    session = make_session()
    add(session, datetime(2026, 6, 1), SELL_CALL, 5, 40.0, date(2026, 6, 5), 500.0)
    add(session, datetime(2026, 6, 4), BUY_CALL, 5, 40.0, date(2026, 6, 5), -100.0)
    add(session, datetime(2026, 6, 9), SELL_PUT, 2, 35.0, date(2026, 6, 19), 100.0)
    session.commit()

    rounds = build_trade_rounds(session, as_of=datetime(2026, 6, 10))
    stats = summarize_range(session, rounds, datetime(2026, 6, 1), datetime(2026, 6, 10), base_value=100_000.0)

    assert stats.premium_collected == 600.0
    assert stats.buyback_cost == 100.0
    assert stats.net_premium == 500.0
    assert stats.days == 10
    assert stats.annualized_dollars == 500.0 / 10 * 365
    assert round(stats.annualized_pct, 2) == round(stats.annualized_dollars / 100_000.0 * 100, 2)
    assert stats.realized_pnl == 400.0  # only the bought-back round closed inside the range
    assert stats.closed_rounds == 1
    assert stats.open_rounds == 1
    assert stats.open_premium == 100.0

    # A range before the put sale only sees the call round.
    early = rounds_in_range(rounds, datetime(2026, 6, 1), datetime(2026, 6, 5))
    assert len(early) == 1

    span = journal_date_span(session)
    assert span == (datetime(2026, 6, 1), datetime(2026, 6, 9))
